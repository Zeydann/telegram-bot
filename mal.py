import re
import html as html_lib
import httpx

MAL_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
}

STATUS_MAP = {
    "Currently Airing": "Ongoing",
    "Finished Airing": "Complete",
    "Not yet aired": "Upcoming",
}


def _strip_tags(text: str) -> str:
    return html_lib.unescape(re.sub(r"<[^>]+>", " ", text)).strip()


def _page_field(page: str, label: str) -> str | None:
    m = re.search(r'<span class="dark_text">' + re.escape(label) + r":</span>\s*([^<]+)", page)
    return m.group(1).strip() if m else None


async def search_anime(query: str) -> dict | None:
    """Cari anime di MyAnimeList, lalu ambil detailnya. None kalau tidak ketemu."""
    async with httpx.AsyncClient(headers=MAL_HEADERS, timeout=20, follow_redirects=True) as client:
        search = await client.get("https://myanimelist.net/anime.php", params={"q": query, "cat": "anime"})
        links = re.findall(r'href="(https://myanimelist\.net/anime/\d+/[^"/]+)"', search.text)
        if not links:
            return None

        detail = await client.get(links[0])
        page = detail.text

    m = re.search(r'<h1[^>]*class="title-name[^"]*"[^>]*>(.*?)</h1>', page, re.S)
    title = _strip_tags(m.group(1)) if m else links[0].rsplit("/", 1)[-1].replace("_", " ")

    episodes = _page_field(page, "Episodes")
    status_raw = _page_field(page, "Status")
    status = STATUS_MAP.get(status_raw or "", status_raw or "Unknown")

    m = re.search(r'itemprop="ratingValue"[^>]*>([\d.]+)<', page)
    score = m.group(1) if m else "N/A"

    m = re.search(r'<p itemprop="description">(.*?)</p>', page, re.S)
    synopsis = _strip_tags(m.group(1)) if m else None
    if synopsis and len(synopsis) > 350:
        synopsis = synopsis[:347].rsplit(" ", 1)[0] + "..."

    return {
        "title": title,
        "episodes": episodes or "?",
        "status": status,
        "score": score,
        "synopsis": synopsis or "Tidak ada deskripsi.",
        "url": links[0],
    }
