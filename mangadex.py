"""Provider MangaDex untuk fitur manga & manhwa Bahasa Indonesia.

Sumber: API resmi MangaDex (https://api.mangadex.org, dokumentasi:
https://api.mangadex.org/docs/). Kebijakan penggunaan yang berlaku
(Acceptable Usage Policy di halaman dokumen) mensyaratkan:
  1. wajib mencantumkan kredit MangaDex,
  2. wajib mencantumkan kredit grup scanlation (dan menghormati permintaan
     penghapusan konten dari grup),
  3. tidak boleh memasang iklan atau layanan berbayar berbasis API ini.

Modul ini HANYA memakai endpoint publik yang terdokumentasi, dengan:
  - rate limit sendiri (maksimal ~4 request/detik, di bawah batas 5/detik),
  - penanganan 429 (hormati header ``retry-after``) dan error sementara,
  - cache TTL untuk mengurangi jumlah request berulang.

Filter Bahasa Indonesia: setiap daftar chapter selalu diminta dengan parameter
``translatedLanguage[]=id`` dan hasilnya dicek ulang di sisi klien (kadang API
mengembalikan campuran). Tidak ada fallback ke bahasa Inggris di modul ini.

Catatan: filter ketersediaan ``availableTranslatedLanguages`` pada objek manga
TERBUKTI basi (mis. judul yang mencantumkan ``id`` tapi chapter feed-nya nol),
jadi ketersediaan chapter Indonesia SELALU diverifikasi lewat chapter feed.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)

BASE_API = "https://api.mangadex.org"

# Kredit sumber — wajib tampil pada pesan hasil (syarat penggunaan API MangaDex).
SOURCE_NAME = "MangaDex"
WEBSITE_URL = "https://mangadex.org/"
API_DOCS_URL = "https://api.mangadex.org/docs/"

INDONESIAN_LANG = "id"

HEADERS = {
    "User-Agent": (
        "telegram-bot-manga/1.0 (MangaDex API client; "
        "https://github.com/Zeydann/telegram-bot)"
    )
}
REQUEST_TIMEOUT = 20.0
IMAGE_TIMEOUT = 40.0
MIN_REQUEST_INTERVAL = 0.25  # ~4 req/detik; batas resmi MangaDex 5 req/detik
MAX_RETRIES = 3

FEED_LIMIT = 500              # batas maksimal limit feed chapter
MAX_FEED_PAGES = 3            # 3 x 500 = 1500 chapter teratas (daftar bisa terpotong)
SEARCH_LIMIT = 25             # hasil pencarian judul per request
MAX_VERIFY = 8                # maksimal judul yang diverifikasi per pencarian
MAX_RELATED_TITLES = 6        # maksimal judul siap tampil per pencarian
CONTENT_RATING = ("safe", "suggestive")

TTL_SEARCH = 300
TTL_DETAILS = 600
TTL_STATS = 300
TTL_CHAPTERS = 600
CACHE_MAX_ENTRIES = 256


class MangaDexError(Exception):
    """Kelas dasar error provider; pesannya aman ditampilkan ke pengguna."""


class SourceUnavailable(MangaDexError):
    """Sumber tidak dapat diakses (jaringan/HTTP error/balasan tidak valid)."""


class SourceRateLimited(MangaDexError):
    """Terkena rate limit API setelah beberapa kali percobaan ulang."""


class TTLCache:
    """Cache sederhana ber-TTL dengan batas jumlah entri."""

    def __init__(self, ttl: float, max_entries: int = CACHE_MAX_ENTRIES):
        self.ttl = ttl
        self.max_entries = max_entries
        self._data: dict[str, tuple[float, object]] = {}

    def get(self, key: str):
        entry = self._data.get(key)
        if not entry:
            return None
        expires, value = entry
        if time.monotonic() > expires:
            self._data.pop(key, None)
            return None
        return value

    def set(self, key: str, value) -> None:
        if len(self._data) >= self.max_entries:
            # buang entri kedaluwarsa dulu, kalau masih penuh buang acak
            now = time.monotonic()
            expired = [k for k, (exp, _) in self._data.items() if now > exp]
            for k in expired:
                self._data.pop(k, None)
            while len(self._data) >= self.max_entries:
                self._data.pop(next(iter(self._data)))
        self._data[key] = (time.monotonic() + self.ttl, value)


_cache_search = TTLCache(TTL_SEARCH)
_cache_details = TTLCache(TTL_DETAILS)
_cache_stats = TTLCache(TTL_STATS)
_cache_chapters = TTLCache(TTL_CHAPTERS)

_client: httpx.AsyncClient | None = None
_throttle_lock = asyncio.Lock()
_last_request_at = 0.0


def _get_client() -> httpx.AsyncClient:
    """Client async bersama; dibuat lazily supaya aman untuk event loop mana pun."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT,
            follow_redirects=True,
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


async def _throttle() -> None:
    """Jaga jarak minimal antar-request ke API ( hormati rate limit MangaDex)."""
    global _last_request_at
    async with _throttle_lock:
        now = time.monotonic()
        wait = MIN_REQUEST_INTERVAL - (now - _last_request_at)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_request_at = time.monotonic()


async def _get(path: str, params: dict | None = None) -> dict:
    """GET JSON ke API MangaDex dengan retry untuk 429/5xx/timeout."""
    url = BASE_API + path
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        await _throttle()
        try:
            resp = await _get_client().get(url, params=params)
        except httpx.HTTPError as exc:
            last_error = exc
            logger.warning("MangaDex request gagal (%s), percobaan %d: %s",
                           path, attempt + 1, type(exc).__name__)
            await asyncio.sleep(2 ** attempt)
            continue

        if resp.status_code == 429:
            try:
                retry_after = float(resp.headers.get("retry-after", "2"))
            except ValueError:
                retry_after = 2.0
            logger.info("MangaDex 429, menunggu %.1fs: %s", retry_after, path)
            await asyncio.sleep(min(max(retry_after, 1.0), 15.0))
            continue
        if resp.status_code >= 500:
            last_error = SourceUnavailable(f"HTTP {resp.status_code}")
            await asyncio.sleep(2 ** attempt)
            continue
        if resp.status_code >= 400:
            # 400/404/403 dari API berarti permintaan kita tidak valid atau
            # sumber menyembunyikan resource — jangan dicoba ulang.
            logger.warning("MangaDex HTTP %d untuk %s", resp.status_code, path)
            raise SourceUnavailable(f"HTTP {resp.status_code}")

        try:
            data = resp.json()
        except ValueError as exc:
            raise SourceUnavailable("Balasan bukan JSON") from exc
        if not isinstance(data, dict):
            raise SourceUnavailable("Format balasan tidak dikenal")
        return data

    logger.error("MangaDex menolak request setelah %d percobaan: %s (%s)",
                 MAX_RETRIES, path, last_error)
    raise SourceRateLimited("Sedang dibatasi oleh sumber. Coba lagi nanti.")


# ---------------------------------------------------------------------------
# Model data
# ---------------------------------------------------------------------------

@dataclass
class Title:
    """Ringkasan judul manga/manhwa beserta ketersediaan chapter Indonesia."""
    id: str
    title: str
    indonesian_title: str | None = None
    alt_titles: list[str] = field(default_factory=list)
    synopsis: str = ""
    synopsis_id: str | None = None
    genres: list[str] = field(default_factory=list)
    status: str = "Tidak diketahui"
    original_language: str = "Tidak diketahui"
    category: str = ""          # "manga" | "manhwa"
    cover_url: str | None = None
    url: str = ""
    author: str | None = None
    id_chapter_count: int | None = None   # jumlah chapter Bahasa Indonesia
    latest_id_chapter: str | None = None  # nomor chapter Indonesia terbaru
    chapters_truncated: bool = False      # daftar chapter terpotong di batas unduh


@dataclass
class Chapter:
    """Satu rilisan chapter (selalu Bahasa Indonesia pada modul ini)."""
    id: str
    number: str | None
    title: str | None
    language: str
    group: str | None
    publish_at: str | None
    external_url: str | None
    pages: int
    url: str

    @property
    def label(self) -> str:
        return f"Ch. {self.number}" if self.number else "Oneshot"


# ---------------------------------------------------------------------------
# Utilitas parsing
# ---------------------------------------------------------------------------

STATUS_ID = {
    "ongoing": "Berjalan",
    "completed": "Tamat",
    "cancelled": "Dibatalkan",
    "hiatus": "Hiatus",
}

LANGUAGE_NAMES = {
    "ja": "Jepang",
    "ko": "Korea",
    "zh": "Mandarin",
    "zh-hk": "Mandarin (HK)",
    "en": "Inggris",
    "id": "Indonesia",
}


def classify(original_language: str | None) -> str | None:
    """Klasifikasi kategori dari metadata bahasa asal (bukan dari nama judul).

    MangaDex menyimpan ``originalLanguage`` (ISO 639-1); ja = manga,
    ko = manhwa. Tanpa metadata yang andal, judul tidak dikategorikan.
    """
    if not original_language:
        return None
    lang = original_language.lower()
    if lang.startswith("ja"):
        return "manga"
    if lang.startswith("ko"):
        return "manhwa"
    return None


def language_name(code: str) -> str:
    return LANGUAGE_NAMES.get(code, code)


def manga_url(manga_id: str) -> str:
    return f"{WEBSITE_URL}title/{manga_id}"


def chapter_url(chapter_id: str) -> str:
    return f"{WEBSITE_URL}chapter/{chapter_id}"


def cover_url(manga_id: str, file_name: str, size: int = 512) -> str:
    """URL cover dari CDN MangaDex (ukuran 256/512/1024)."""
    return f"https://uploads.mangadex.org/covers/{manga_id}/{file_name}.{size}.jpg"


def _localized(mapping: dict | None, key: str) -> str | None:
    if not mapping:
        return None
    value = mapping.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _pick_title(attrs: dict) -> str:
    """Judul utama: utamakan bahasa Inggris, lalu romanisasi Jepang/Korea."""
    titles = attrs.get("title") or {}
    for lang in ("en", "id", "ja-ro", "ko-ro"):
        value = _localized(titles, lang)
        if value:
            return value
    for value in titles.values():
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "Tanpa judul"


def _pick_alt_titles(attrs: dict, exclude: str, limit: int = 4) -> list[str]:
    alt: list[str] = []
    for entry in attrs.get("altTitles") or []:
        if not isinstance(entry, dict):
            continue
        for lang in ("en", "id", "ja-ro", "ko-ro", "ja", "ko"):
            value = _localized(entry, lang)
            if value and value != exclude and value not in alt:
                alt.append(value)
                break
        if len(alt) >= limit:
            break
    return alt


def _pick_indonesian_title(attrs: dict) -> str | None:
    """Judul berbahasa Indonesia hanya jika metadata-nya benar-benar ada."""
    value = _localized(attrs.get("title"), INDONESIAN_LANG)
    if value:
        return value
    for entry in attrs.get("altTitles") or []:
        if isinstance(entry, dict):
            value = _localized(entry, INDONESIAN_LANG)
            if value:
                return value
    return None


def _pick_description(attrs: dict, language: str = "en") -> str | None:
    descriptions = attrs.get("description") or {}
    value = _localized(descriptions, language)
    if value:
        return value
    for value in descriptions.values():
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _pick_genres(attrs: dict) -> list[str]:
    genres: list[str] = []
    for tag in attrs.get("tags") or []:
        attributes = (tag or {}).get("attributes") or {}
        name = _localized(attributes.get("name"), "en") or _localized(
            attributes.get("name"), INDONESIAN_LANG
        )
        if name:
            genres.append(name)
    return genres


def _pick_author(manga_json: dict) -> str | None:
    for rel in manga_json.get("relationships") or []:
        if (rel or {}).get("type") == "author":
            name = (rel.get("attributes") or {}).get("name")
            if name:
                return name
    return None


def _pick_cover(manga_json: dict, size: int = 512) -> str | None:
    for rel in manga_json.get("relationships") or []:
        if (rel or {}).get("type") == "cover_art":
            file_name = (rel.get("attributes") or {}).get("fileName")
            if file_name:
                return cover_url(manga_json["id"], file_name, size)
    return None


def parse_title(manga_json: dict) -> Title:
    attrs = manga_json.get("attributes") or {}
    title = _pick_title(attrs)
    original_language = attrs.get("originalLanguage") or ""
    return Title(
        id=manga_json["id"],
        title=title,
        indonesian_title=_pick_indonesian_title(attrs),
        alt_titles=_pick_alt_titles(attrs, exclude=title),
        synopsis=_pick_description(attrs) or "",
        synopsis_id=_pick_description(attrs, INDONESIAN_LANG),
        genres=_pick_genres(attrs),
        status=STATUS_ID.get((attrs.get("status") or "").lower(), "Tidak diketahui"),
        original_language=language_name(original_language) if original_language
        else "Tidak diketahui",
        category=classify(original_language) or "",
        cover_url=_pick_cover(manga_json),
        url=manga_url(manga_json["id"]),
        author=_pick_author(manga_json),
    )


def parse_chapter(chapter_json: dict) -> Chapter:
    attrs = chapter_json.get("attributes") or {}
    group_name = None
    external_url = attrs.get("externalUrl")
    for rel in chapter_json.get("relationships") or []:
        if (rel or {}).get("type") == "scanlation_group":
            group_name = (rel.get("attributes") or {}).get("name")
            break
    return Chapter(
        id=chapter_json["id"],
        number=attrs.get("chapter"),
        title=(attrs.get("title") or "").strip() or None,
        language=attrs.get("translatedLanguage") or "",
        group=group_name,
        publish_at=attrs.get("publishAt"),
        external_url=external_url or None,
        pages=int(attrs.get("pages") or 0),
        url=chapter_url(chapter_json["id"]),
    )


def chapter_number_value(number: str | None) -> float:
    """Nilai numerik nomor chapter untuk pengurutan; tidak terbaca = besar."""
    try:
        return float(number)
    except (TypeError, ValueError):
        return float("inf")


def sort_key(chapter: Chapter) -> tuple:
    value = chapter_number_value(chapter.number)
    return (value if value != float("inf") else float("inf"), chapter.id)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

async def search_titles(query: str, category: str | None = None,
                        limit: int = SEARCH_LIMIT) -> list[Title]:
    """Cari judul di MangaDex (relevansi), difilter kategori bila diminta.

    Kategori ditentukan dari metadata ``originalLanguage``, bukan dari nama.
    """
    cache_key = f"{query}|{category or '*'}|{limit}"
    cached = _cache_search.get(cache_key)
    if cached is not None:
        return cached

    params = {
        "title": query,
        "limit": min(limit, SEARCH_LIMIT),
        "order[relevance]": "desc",
        "includes[]": ["cover_art", "author"],
        "contentRating[]": list(CONTENT_RATING),
    }
    data = await _get("/manga", params)
    titles: list[Title] = []
    for manga_json in data.get("data") or []:
        try:
            title = parse_title(manga_json)
        except Exception:
            logger.exception("Gagal memparsing entri manga dari pencarian %r", query)
            continue
        if category and title.category != category:
            continue
        titles.append(title)

    _cache_search.set(cache_key, titles)
    return titles


async def indonesian_stats(manga_id: str) -> tuple[int, str | None]:
    """(jumlah chapter Indonesia, nomor terbaru) — verifikasi via feed.

    Ini SATU-SATUNYA cara yang andal mengetahui ketersediaan chapter
    Bahasa Indonesia sebuah judul (metadata ``availableTranslatedLanguages``
    terbukti basi).
    """
    cached = _cache_stats.get(manga_id)
    if cached is not None:
        return cached

    params = {
        "limit": 1,
        "translatedLanguage[]": [INDONESIAN_LANG],
        "order[chapter]": "desc",
        "contentRating[]": list(CONTENT_RATING),
    }
    data = await _get(f"/manga/{manga_id}/feed", params)
    total = int(data.get("total") or 0)
    latest = None
    for chapter_json in data.get("data") or []:
        attrs = chapter_json.get("attributes") or {}
        # pertahankan hanya Bahasa Indonesia (defensif terhadap API)
        if attrs.get("translatedLanguage") != INDONESIAN_LANG:
            continue
        latest = attrs.get("chapter")
    result = (total, latest)
    _cache_stats.set(manga_id, result)
    return result


async def find_titles_with_indonesian(query: str, category: str | None = None,
                                      max_verify: int = MAX_VERIFY,
                                      require_category: bool = True,
                                      max_results: int = MAX_RELATED_TITLES) -> list[Title]:
    """Cari judul yang punya chapter Bahasa Indonesia (hasil diverifikasi feed).

    Setiap kandidat diverifikasi dengan ``translatedLanguage[]=id``; judul
    tanpa chapter Indonesia dilewati dan TIDAK pernah diganti judul lain
    berbahasa Inggris.
    """
    cache_key = f"find|{query}|{category or '*'}"
    cached = _cache_search.get(cache_key)
    if cached is not None:
        return cached

    candidates = await search_titles(query, category=category if require_category else None)
    found: list[Title] = []
    verified = 0
    for title in candidates:
        if len(found) >= max_results or verified >= max_verify:
            break
        if require_category and category and title.category != category:
            continue
        verified += 1
        total, latest = await indonesian_stats(title.id)
        if total <= 0:
            continue
        title.id_chapter_count = total
        title.latest_id_chapter = latest
        found.append(title)

    _cache_search.set(cache_key, found)
    return found


async def get_manga(manga_id: str) -> Title:
    """Detail satu judul (cover, penulis, genre, sinopsis, dst.)."""
    cached = _cache_details.get(manga_id)
    if cached is not None:
        return cached
    data = await _get(f"/manga/{manga_id}", {
        "includes[]": ["cover_art", "author", "artist"],
        "contentRating[]": list(CONTENT_RATING),
    })
    title = parse_title(data.get("data") or {})
    _cache_details.set(manga_id, title)
    return title


async def get_indonesian_chapters(manga_id: str) -> tuple[list[Chapter], bool]:
    """Seluruh chapter Bahasa Indonesia untuk satu judul, urut baca.

    Mengambil maksimal ``MAX_FEED_PAGES`` x ``FEED_LIMIT`` chapter; bila
    lebih, daftar dianggap terpotong (flag kedua pada tuple). Hanya chapter
    dengan ``translatedLanguage == "id"`` yang dipertahankan.
    """
    cache_key = f"chapters|{manga_id}"
    cached = _cache_chapters.get(cache_key)
    if cached is not None:
        return cached

    chapters: list[Chapter] = []
    truncated = False
    for page in range(MAX_FEED_PAGES):
        params = {
            "limit": FEED_LIMIT,
            "offset": page * FEED_LIMIT,
            "translatedLanguage[]": [INDONESIAN_LANG],
            "order[chapter]": "asc",
            "includes[]": ["scanlation_group"],
            "contentRating[]": list(CONTENT_RATING),
        }
        data = await _get(f"/manga/{manga_id}/feed", params)
        batch = data.get("data") or []
        for chapter_json in batch:
            attrs = chapter_json.get("attributes") or {}
            if attrs.get("translatedLanguage") != INDONESIAN_LANG:
                continue  # jangan pernah menampilkan chapter non-Indonesia
            chapters.append(parse_chapter(chapter_json))
        total = int(data.get("total") or 0)
        if len(batch) < FEED_LIMIT or page * FEED_LIMIT + len(batch) >= total:
            break
        if page == MAX_FEED_PAGES - 1:
            truncated = True

    chapters.sort(key=sort_key)
    result = (chapters, truncated)
    _cache_chapters.set(cache_key, result)
    return result


async def get_releases(manga_id: str, number: float) -> list[Chapter]:
    """Rilisan Bahasa Indonesia untuk nomor chapter tertentu (bisa > 1 grup)."""
    chapters, _truncated = await get_indonesian_chapters(manga_id)
    matches: list[Chapter] = []
    for chapter in chapters:
        try:
            value = float(chapter.number)
        except (TypeError, ValueError):
            continue
        if abs(value - number) < 1e-6:
            matches.append(chapter)
    return matches


async def get_pages(chapter_id: str) -> list[str] | None:
    """URL halaman chapter dari MangaDex@Home; None bila tidak tersedia."""
    data = await _get(f"/at-home/server/{chapter_id}")
    chapter = data.get("chapter") or {}
    base_url = data.get("baseUrl")
    file_hash = chapter.get("hash")
    files = chapter.get("data") or []
    if not base_url or not file_hash or not files:
        return None
    return [f"{base_url}/data/{file_hash}/{name}" for name in files]


async def download_image(url: str) -> bytes | None:
    """Unduh satu gambar halaman; None bila gagal atau bukan gambar."""
    try:
        resp = await _get_client().get(url, timeout=IMAGE_TIMEOUT)
    except httpx.HTTPError as exc:
        logger.warning("Gagal unduh halaman chapter: %s", type(exc).__name__)
        return None
    if resp.status_code != 200:
        logger.warning("Unduh halaman chapter HTTP %d", resp.status_code)
        return None
    content_type = resp.headers.get("content-type", "")
    if not content_type.startswith("image/"):
        logger.warning("Bukan gambar: %s", content_type)
        return None
    return resp.content


def source_credit() -> str:
    """Kredit sumber resmi (syarat penggunaan API MangaDex)."""
    return (f"Source: {SOURCE_NAME}\n"
            f"Website: {WEBSITE_URL}\n"
            f"API Documentation: {API_DOCS_URL}")
