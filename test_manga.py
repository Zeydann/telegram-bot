"""Tests untuk fitur manga & manhwa (Indonesia-first + penawaran versi Inggris).

Jalankan dengan:

    venv/bin/python test_manga.py

Semua test berjalan tanpa jaringan: panggilan provider di-mock.
"""

import asyncio
import unittest
from unittest import mock

import manga
import mangadex
import bot


def async_return(value):
    """Buat fungsi async yang selalu mengembalikan `value` (untuk patch)."""

    async def _inner(*args, **kwargs):
        return value
    return _inner


def asyncio_run(coro):
    return asyncio.run(coro)


def make_title(manga_id="m1", title="One Piece", category="manga",
               id_count=10, latest="10", **kwargs):
    data = dict(
        id=manga_id,
        title=title,
        indonesian_title=kwargs.pop("indonesian_title", None),
        alt_titles=kwargs.pop("alt_titles", ["OP"]),
        synopsis=kwargs.pop("synopsis", "Petualangan bajak laut."),
        synopsis_id=kwargs.pop("synopsis_id", None),
        genres=kwargs.pop("genres", ["Action"]),
        status=kwargs.pop("status", "Berjalan"),
        original_language=kwargs.pop("original_language", "Jepang"),
        category=category,
        cover_url=kwargs.pop("cover_url", "https://covers/cover.jpg"),
        url=mangadex.manga_url(manga_id),
        author=kwargs.pop("author", "Oda"),
        id_chapter_count=id_count,
        latest_id_chapter=latest,
    )
    data.update(kwargs)
    return mangadex.Title(**data)


def make_chapter(chapter_id="c1", number="10", language="id", group="Grup A",
                 title="Judul", external_url=None):
    return mangadex.Chapter(
        id=chapter_id,
        number=number,
        title=title,
        language=language,
        group=group,
        publish_at="2026-01-01T00:00:00+00:00",
        external_url=external_url,
        pages=10,
        url=mangadex.chapter_url(chapter_id),
    )


def chapter_json(chapter):
    relationship = ({"type": "scanlation_group",
                     "attributes": {"name": chapter.group}} if chapter.group
                    else {"type": "scanlation_group"})
    return {"id": chapter.id, "attributes": {
        "chapter": chapter.number, "title": chapter.title,
        "translatedLanguage": chapter.language, "pages": chapter.pages,
        "publishAt": chapter.publish_at, "externalUrl": chapter.external_url},
        "relationships": [relationship]}


# ---------------------------------------------------------------------------
# Fakes Telegram (semua balasan/anak pesan tercatat di root message)
# ---------------------------------------------------------------------------

class FakeMessage:
    def __init__(self, text=None, log=None):
        self.text = text
        self.photo = None
        self.log = log if log is not None else self
        if self.log is self:
            self.log._replies = []
            self.log._edits = []
            self.log._deleted = False

    @property
    def replies(self):
        return self.log._replies

    @property
    def edits(self):
        return self.log._edits

    @property
    def deleted(self):
        return self.log._deleted

    async def reply_text(self, text, **kwargs):
        self.log._replies.append({"text": text, **kwargs})
        return FakeMessage(text=text, log=self.log)

    async def reply_photo(self, photo, caption=None, **kwargs):
        self.log._replies.append({"photo": photo, "caption": caption, **kwargs})
        return FakeMessage(text=caption, log=self.log)

    async def reply_media_group(self, media, **kwargs):
        self.log._replies.append({"media_group": media, **kwargs})
        return media

    async def reply_document(self, document=None, **kwargs):
        self.log._replies.append({"document": document, **kwargs})
        return FakeMessage(log=self.log)

    async def edit_text(self, text, **kwargs):
        self.log._edits.append({"text": text, **kwargs})
        self.text = text
        return self

    async def delete(self):
        self.log._deleted = True


class FakeCallbackQuery:
    def __init__(self, data, message=None):
        self.data = data
        self.message = message or FakeMessage(text="pesan hasil")
        self.answers = []
        self.edits = []

    async def answer(self, text=None, **kwargs):
        self.answers.append(text)

    async def edit_message_text(self, text, **kwargs):
        self.edits.append({"text": text, **kwargs})
        self.message.text = text

    async def edit_message_caption(self, caption, **kwargs):
        self.edits.append({"caption": caption, **kwargs})


def make_update(message, user_id=111):
    return mock.Mock(message=message,
                     effective_user=type("User", (), {"id": user_id})())


def make_callback_update(callback_query, user_id=111):
    return mock.Mock(callback_query=callback_query,
                     effective_user=type("User", (), {"id": user_id})())


def make_context(args):
    return mock.Mock(args=args)


def last_reply_text(message):
    for entry in reversed(message.replies):
        if "text" in entry:
            return entry["text"]
    return ""


def last_edit_text(message):
    for entry in reversed(message.edits):
        if "text" in entry:
            return entry["text"]
    return ""


def last_reply_body(message):
    """Caption foto terakhir, atau pesan teks terakhir, dari balasan bot."""
    for entry in reversed(message.replies):
        if entry.get("caption"):
            return entry["caption"]
        if "text" in entry:
            return entry["text"]
    return ""


# ---------------------------------------------------------------------------
# Provider: filter Bahasa Indonesia (inti fitur)
# ---------------------------------------------------------------------------

class IndonesianFilterTests(unittest.TestCase):
    def setUp(self):
        """Bersihkan cache provider supaya hasil mock tidak bocor antar test."""
        for cache in (mangadex._cache_search, mangadex._cache_details,
                      mangadex._cache_stats, mangadex._cache_chapters):
            cache._data.clear()

    def test_feed_keeps_only_indonesian(self):
        """Balasan API berisi campuran bahasa -> hanya 'id' dipertahankan."""
        mixed = [
            {"id": "a", "attributes": {"chapter": "1", "translatedLanguage": "id",
                                       "pages": 5}},
            {"id": "b", "attributes": {"chapter": "2", "translatedLanguage": "en",
                                       "pages": 5}},
            {"id": "c", "attributes": {"chapter": "3", "translatedLanguage": "id",
                                       "pages": 5}},
            {"id": "d", "attributes": {"chapter": "4", "translatedLanguage": "pt-br",
                                       "pages": 5}},
        ]

        async def fake_get(path, params=None):
            self.assertEqual(params["translatedLanguage[]"], ["id"])
            return {"data": mixed, "total": len(mixed)}

        with mock.patch.object(mangadex, "_get", fake_get):
            chapters, truncated = asyncio_run(mangadex.get_indonesian_chapters("m1"))

        self.assertEqual([c.id for c in chapters], ["a", "c"])
        self.assertFalse(truncated)
        for chapter in chapters:
            self.assertEqual(chapter.language, "id")

    def test_stats_zero_when_no_indonesian(self):
        """Ketersediaan dihitung dari feed id, bukan metadata basi."""
        async def fake_get(path, params=None):
            self.assertEqual(params["translatedLanguage[]"], ["id"])
            return {"data": [], "total": 0}

        with mock.patch.object(mangadex, "_get", fake_get):
            total, latest = asyncio_run(mangadex.indonesian_stats("m1"))
        self.assertEqual((total, latest), (0, None))

    def test_search_filters_category_by_metadata_not_title(self):
        payload = {"data": [
            {"id": "a", "attributes": {"title": {"en": "Judul Korea"},
                                       "originalLanguage": "ko", "altTitles": [],
                                       "description": {}, "tags": [], "status": "ongoing"},
             "relationships": []},
            {"id": "b", "attributes": {"title": {"en": "Judul Jepang"},
                                       "originalLanguage": "ja", "altTitles": [],
                                       "description": {}, "tags": [], "status": "ongoing"},
             "relationships": []},
        ]}

        async def fake_get(path, params=None):
            self.assertEqual(params["contentRating[]"], ["safe", "suggestive"])
            return payload

        with mock.patch.object(mangadex, "_get", fake_get):
            manga_titles = asyncio_run(mangadex.search_titles("Judul", category="manga"))
            manhwa_titles = asyncio_run(mangadex.search_titles("Judul", category="manhwa"))

        self.assertEqual([t.id for t in manga_titles], ["b"])
        self.assertEqual([t.id for t in manhwa_titles], ["a"])

    def test_classify_uses_original_language(self):
        self.assertEqual(mangadex.classify("ja"), "manga")
        self.assertEqual(mangadex.classify("ja-ro"), "manga")
        self.assertEqual(mangadex.classify("ko"), "manhwa")
        self.assertIsNone(mangadex.classify("en"))
        self.assertIsNone(mangadex.classify(None))
        self.assertIsNone(mangadex.classify("zh"))

    def test_find_titles_skips_titles_without_indonesian(self):
        """Judul tanpa chapter id dilewati; tidak diganti judul lain."""
        search_payload = {"data": [
            {"id": "no-id", "attributes": {"title": {"en": "A"},
                                           "originalLanguage": "ja", "altTitles": [],
                                           "description": {}, "tags": [],
                                           "status": "ongoing"}, "relationships": []},
            {"id": "has-id", "attributes": {"title": {"en": "B"},
                                            "originalLanguage": "ja", "altTitles": [],
                                            "description": {}, "tags": [],
                                            "status": "ongoing"}, "relationships": []},
        ]}

        async def fake_get(path, params=None):
            if path == "/manga":
                return search_payload
            if params.get("translatedLanguage[]") == ["id"]:
                has_id = path.endswith("has-id/feed")
                return {"total": 7 if has_id else 0,
                        "data": [{"id": "x", "attributes": {
                            "chapter": "7", "translatedLanguage": "id"}}] if has_id else []}
            raise AssertionError(path)

        with mock.patch.object(mangadex, "_get", fake_get):
            found = asyncio_run(mangadex.find_titles_with_indonesian(
                "B", category="manga"))

        self.assertEqual([t.id for t in found], ["has-id"])
        self.assertEqual(found[0].id_chapter_count, 7)

    def test_get_releases_matches_exact_number(self):
        chapters = [make_chapter("c9", "9"), make_chapter("c10", "10"),
                    make_chapter("c10b", "10", group="Grup B"),
                    make_chapter("c11", "11")]

        async def fake_get(path, params=None):
            return {"data": [chapter_json(c) for c in chapters],
                    "total": len(chapters)}

        with mock.patch.object(mangadex, "_get", fake_get):
            releases = asyncio_run(mangadex.get_releases("m1", 10.0))

        self.assertEqual([c.id for c in releases], ["c10", "c10b"])

    def test_get_releases_empty_without_indonesian(self):
        async def fake_get(path, params=None):
            return {"data": [], "total": 0}

        with mock.patch.object(mangadex, "_get", fake_get):
            self.assertEqual(asyncio_run(mangadex.get_releases("m1", 10.0)), [])

    def test_get_pages_none_when_no_pages(self):
        async def fake_get(path, params=None):
            return {"baseUrl": "https://x", "chapter": {"hash": "", "data": []}}

        with mock.patch.object(mangadex, "_get", fake_get):
            self.assertIsNone(asyncio_run(mangadex.get_pages("c1")))

    def test_get_pages_builds_urls(self):
        async def fake_get(path, params=None):
            return {"baseUrl": "https://cdn",
                    "chapter": {"hash": "h", "data": ["1.jpg", "2.jpg"]}}

        with mock.patch.object(mangadex, "_get", fake_get):
            pages = asyncio_run(mangadex.get_pages("c1"))
        self.assertEqual(pages, ["https://cdn/data/h/1.jpg",
                                 "https://cdn/data/h/2.jpg"])

    def test_source_credit_mentions_mangadex(self):
        credit = mangadex.source_credit()
        self.assertIn("Source: MangaDex", credit)
        self.assertIn("https://mangadex.org/", credit)
        self.assertIn("https://api.mangadex.org/docs/", credit)


# ---------------------------------------------------------------------------
# Handler: validasi input, pencarian, pesan error
# ---------------------------------------------------------------------------

class HandlerValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_manga_requires_query(self):
        message = FakeMessage(text="/manga")
        await manga.manga_cmd(make_update(message), make_context([]))
        self.assertIn("/manga", last_reply_text(message))

    async def test_manhwa_requires_query(self):
        message = FakeMessage(text="/manhwa")
        await manga.manhwa_cmd(make_update(message), make_context([]))
        self.assertIn("/manhwa", last_reply_text(message))

    async def test_comic_requires_query(self):
        message = FakeMessage(text="/comic")
        await manga.comic_cmd(make_update(message), make_context([]))
        self.assertIn("/comic", last_reply_text(message))

    async def test_chapter_requires_query(self):
        message = FakeMessage(text="/chapter")
        await manga.chapter_cmd(make_update(message), make_context([]))
        self.assertIn("/chapter", last_reply_text(message))

    async def test_mangainfo_requires_query(self):
        message = FakeMessage(text="/mangainfo")
        await manga.mangainfo_cmd(make_update(message), make_context([]))
        self.assertIn("/mangainfo", last_reply_text(message))

    async def test_read_requires_title_and_chapter(self):
        message = FakeMessage(text="/read One")
        await manga.read_cmd(make_update(message), make_context(["One"]))
        self.assertIn("/read", last_reply_text(message))

    async def test_read_rejects_non_numeric_chapter(self):
        message = FakeMessage(text="/read One Piece bab")
        await manga.read_cmd(make_update(message),
                             make_context(["One", "Piece", "bab"]))
        self.assertIn("/read", last_reply_text(message))


class SearchHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_manga_not_found_message(self):
        message = FakeMessage(text="/manga Zzz")
        with mock.patch.object(manga, "_resolve_titles", async_return([])), \
                mock.patch.object(mangadex, "search_titles", async_return([])):
            await manga.manga_cmd(make_update(message), make_context(["Zzz"]))
        self.assertIn('Manga "Zzz" tidak ditemukan', last_edit_text(message))

    async def test_manhwa_not_found_message(self):
        message = FakeMessage(text="/manhwa Zzz")
        with mock.patch.object(manga, "_resolve_titles", async_return([])), \
                mock.patch.object(mangadex, "search_titles", async_return([])):
            await manga.manhwa_cmd(make_update(message), make_context(["Zzz"]))
        self.assertIn('Manhwa "Zzz" tidak ditemukan', last_edit_text(message))

    async def test_no_indonesian_offers_english_with_buttons(self):
        """Judul ada tapi tanpa chapter Indonesia -> tawarkan Inggris (belum dicari)."""
        message = FakeMessage(text="/manga Zzz")

        async def must_not_search_english(*args, **kwargs):
            raise AssertionError("pencarian Inggris tidak boleh jalan tanpa konfirmasi")

        with mock.patch.object(manga, "_resolve_titles", async_return([])), \
                mock.patch.object(mangadex, "search_titles",
                                  async_return([make_title("x", "Zzz")])), \
                mock.patch.object(mangadex, "find_titles_with_language",
                                  must_not_search_english):
            await manga.manga_cmd(make_update(message), make_context(["Zzz"]))

        text = last_edit_text(message)
        self.assertIn("Tidak ada chapter Bahasa Indonesia", text)
        self.assertIn('"Zzz"', text)
        self.assertIn("Source: MangaDex", text)
        markup = message.edits[-1]["reply_markup"]
        labels = [btn.text for row in markup.inline_keyboard for btn in row]
        self.assertEqual(labels, ["Yes, English", "No, Cancel"])

    async def test_search_sends_photo_result_with_buttons(self):
        message = FakeMessage(text="/manga One Piece")
        title = make_title(cover_url="https://covers/one.jpg")
        with mock.patch.object(manga, "_resolve_titles", async_return([title])):
            await manga.manga_cmd(make_update(message),
                                  make_context(["One", "Piece"]))

        photos = [r for r in message.replies if "photo" in r]
        self.assertEqual(len(photos), 1)
        caption = photos[0]["caption"]
        self.assertIn("One Piece", caption)
        self.assertIn("Source: MangaDex", caption)
        self.assertIn("Chapter Indonesia: 10", caption)
        labels = [btn.text for row in photos[0]["reply_markup"].inline_keyboard
                  for btn in row]
        self.assertIn("📖 View Chapters", labels)
        self.assertIn("ℹ️ Manga Info", labels)
        self.assertIn("🌐 Open Source", labels)

    async def test_search_falls_back_to_text_without_cover(self):
        message = FakeMessage(text="/manga One Piece")
        title = make_title(cover_url=None)
        with mock.patch.object(manga, "_resolve_titles", async_return([title])):
            await manga.manga_cmd(make_update(message),
                                  make_context(["One", "Piece"]))

        self.assertFalse([r for r in message.replies if "photo" in r])
        self.assertIn("One Piece", last_reply_text(message))

    async def test_search_caption_is_escaped(self):
        message = FakeMessage(text="/manga X")
        title = make_title(title="<b>Judul & Kawan</b>",
                           synopsis="<script>x</script>")
        with mock.patch.object(manga, "_resolve_titles", async_return([title])):
            await manga.manga_cmd(make_update(message), make_context(["X"]))

        caption = [r for r in message.replies if "photo" in r][0]["caption"]
        self.assertIn("&lt;b&gt;Judul &amp; Kawan&lt;/b&gt;", caption)
        self.assertNotIn("<script>", caption)

    async def test_manhwa_info_button_label(self):
        message = FakeMessage(text="/manhwa Solo")
        title = make_title(category="manhwa", original_language="Korea")

        async def fake_resolve(query, category):
            self.assertEqual(category, "manhwa")
            return [title]

        with mock.patch.object(manga, "_resolve_titles", fake_resolve):
            await manga.manhwa_cmd(make_update(message), make_context(["Solo"]))

        keyboard = [r for r in message.replies if "photo" in r][0]["reply_markup"]
        labels = [btn.text for row in keyboard.inline_keyboard for btn in row]
        self.assertIn("ℹ️ Manhwa Info", labels)

    async def test_comic_shows_category(self):
        message = FakeMessage(text="/comic Solo")
        title = make_title(category="manhwa", original_language="Korea")

        async def fake_resolve(query, category):
            self.assertIsNone(category)  # /comic tidak membatasi kategori
            return [title]

        with mock.patch.object(manga, "_resolve_titles", fake_resolve):
            await manga.comic_cmd(make_update(message), make_context(["Solo"]))

        caption = [r for r in message.replies if "photo" in r][0]["caption"]
        self.assertIn("(Manhwa)", caption)

    async def test_next_results_button_when_multiple(self):
        message = FakeMessage(text="/manga One Piece")
        titles = [make_title("a", "One Piece"), make_title("b", "One Piece Z")]
        with mock.patch.object(manga, "_resolve_titles", async_return(titles)):
            await manga.manga_cmd(make_update(message),
                                  make_context(["One", "Piece"]))

        keyboard = [r for r in message.replies if "photo" in r][0]["reply_markup"]
        labels = [btn.text for row in keyboard.inline_keyboard for btn in row]
        self.assertIn("➡️ Next Results", labels)

    async def test_source_error_shows_indonesian_message(self):
        message = FakeMessage(text="/manga One Piece")
        with mock.patch.object(manga, "_resolve_titles",
                               async_return_error(mangadex.SourceUnavailable("HTTP 500"))):
            await manga.manga_cmd(make_update(message),
                                  make_context(["One", "Piece"]))
        self.assertIn("Sumber sedang tidak dapat diakses", last_edit_text(message))

    async def test_rate_limited_shows_message(self):
        message = FakeMessage(text="/manhwa Solo")
        with mock.patch.object(manga, "_resolve_titles",
                               async_return_error(mangadex.SourceRateLimited("429"))):
            await manga.manhwa_cmd(make_update(message), make_context(["Solo"]))
        self.assertIn("membatasi request", last_edit_text(message))


def async_return_error(exc):
    async def _inner(*args, **kwargs):
        raise exc
    return _inner


# ---------------------------------------------------------------------------
# Daftar chapter
# ---------------------------------------------------------------------------

class ChapterListTests(unittest.IsolatedAsyncioTestCase):
    async def test_chapter_list_shows_only_indonesian(self):
        message = FakeMessage(text="/chapter One Piece")
        chapters = [make_chapter("c1", "1"), make_chapter("c2", "2", group="Grup B")]

        async def fake_chapters(manga_id, lang):
            self.assertEqual(lang, "id")  # daftar chapter selalu Bahasa Indonesia
            return chapters, False

        with mock.patch.object(manga, "_resolve_titles",
                               async_return([make_title()])), \
                mock.patch.object(mangadex, "get_chapters", fake_chapters):
            await manga.chapter_cmd(make_update(message),
                                    make_context(["One", "Piece"]))

        sent = last_reply_text(message)
        self.assertIn("Bahasa: Indonesia (id)", sent)
        self.assertIn("Ch. 1", sent)
        self.assertIn("Grup B", sent)
        self.assertIn("Source: MangaDex", sent)
        keyboard = message.replies[-1]["reply_markup"]
        labels = [btn.text for row in keyboard.inline_keyboard for btn in row]
        self.assertTrue(any(label.startswith("📖 Ch.") for label in labels))

    async def test_chapter_list_marks_multiple_releases(self):
        message = FakeMessage(text="/chapter One Piece")
        chapters = [make_chapter("c1", "1", group="Grup A"),
                    make_chapter("c1b", "1", group="Grup B")]

        async def fake_chapters(manga_id, lang):
            return chapters, False

        with mock.patch.object(manga, "_resolve_titles",
                               async_return([make_title()])), \
                mock.patch.object(mangadex, "get_chapters", fake_chapters):
            await manga.chapter_cmd(make_update(message),
                                    make_context(["One", "Piece"]))

        keyboard = message.replies[-1]["reply_markup"]
        read_buttons = [btn for row in keyboard.inline_keyboard for btn in row
                        if btn.text.startswith("📖 Ch.")]
        self.assertEqual(len(read_buttons), 1)
        self.assertIn("(2 rilis)", read_buttons[0].text)
        self.assertTrue(read_buttons[0].callback_data.startswith("mg|g|"))

    async def test_chapter_list_pagination(self):
        message = FakeMessage(text="/chapter One Piece")
        chapters = [make_chapter(f"c{i}", str(i)) for i in range(1, 21)]

        async def fake_chapters(manga_id, lang):
            return chapters, False

        with mock.patch.object(manga, "_resolve_titles",
                               async_return([make_title()])), \
                mock.patch.object(mangadex, "get_chapters", fake_chapters):
            await manga.chapter_cmd(make_update(message),
                                    make_context(["One", "Piece"]))

        keyboard = message.replies[-1]["reply_markup"]
        labels = [btn.text for row in keyboard.inline_keyboard for btn in row]
        self.assertIn("📄 1/3", labels)
        self.assertTrue(any(btn.callback_data.startswith("mg|c|")
                            for row in keyboard.inline_keyboard for btn in row))

    async def test_chapter_list_notes_truncated_listing(self):
        message = FakeMessage(text="/chapter One Piece")
        chapters = [make_chapter("c1", "1")]

        async def fake_chapters(manga_id, lang):
            return chapters, True

        with mock.patch.object(manga, "_resolve_titles",
                               async_return([make_title()])), \
                mock.patch.object(mangadex, "get_chapters", fake_chapters):
            await manga.chapter_cmd(make_update(message),
                                    make_context(["One", "Piece"]))
        self.assertIn("daftar terpotong", last_reply_text(message))

    async def test_picker_for_multiple_matches(self):
        message = FakeMessage(text="/chapter One Piece")
        titles = [make_title("a", "One Piece"), make_title("b", "One Piece Film")]
        with mock.patch.object(manga, "_resolve_titles", async_return(titles)):
            await manga.chapter_cmd(make_update(message),
                                    make_context(["One", "Piece"]))

        last = message.edits[-1]
        self.assertIn("Silakan pilih salah satu hasil pencarian", last["text"])
        labels = [btn.text for row in last["reply_markup"].inline_keyboard
                  for btn in row]
        self.assertEqual(len(labels), 2)

    def test_group_releases_groups_and_sorts(self):
        chapters = [make_chapter("c2", "2"), make_chapter("c1", "1"),
                    make_chapter("c1b", "1"), make_chapter("cx", None)]
        groups = manga.group_releases(chapters)
        self.assertEqual([key for key, _ in groups], ["2", "1", "oneshot"])
        self.assertEqual(len(groups[1][1]), 2)


# ---------------------------------------------------------------------------
# /read dan callback
# ---------------------------------------------------------------------------

class ReadAndCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_parses_title_and_number(self):
        message = FakeMessage(text="/read One Piece 10")
        seen = {}

        async def fake_resolve(query, category):
            seen["query"] = query
            return [make_title()]

        async def fake_releases(manga_id, number, lang):
            self.assertEqual(lang, "id")  # /read tetap Bahasa Indonesia
            seen["number"] = number
            return [make_chapter("c10", "10")]

        sent = {}

        async def fake_pages(message, title, chapter, lang="id"):
            self.assertEqual(lang, "id")  # /read tetap Bahasa Indonesia
            sent["chapter"] = chapter.id

        with mock.patch.object(manga, "_resolve_titles", fake_resolve), \
                mock.patch.object(mangadex, "get_releases", fake_releases), \
                mock.patch.object(manga, "_send_pages", fake_pages):
            await manga.read_cmd(make_update(message),
                                 make_context(["One", "Piece", "10"]))

        self.assertEqual(seen["query"], "One Piece")
        self.assertEqual(seen["number"], 10.0)
        self.assertEqual(sent["chapter"], "c10")

    async def test_read_not_found_message(self):
        message = FakeMessage(text="/read One Piece 99")
        with mock.patch.object(manga, "_resolve_titles",
                               async_return([make_title()])), \
                mock.patch.object(mangadex, "get_releases", async_return([])):
            await manga.read_cmd(make_update(message),
                                 make_context(["One", "Piece", "99"]))

        joined = str(message.replies) + str(message.edits)
        self.assertIn("Chapter tidak ditemukan", joined)
        self.assertNotIn("English", joined)

    async def test_read_offers_release_picker(self):
        message = FakeMessage(text="/read One Piece 10")
        releases = [make_chapter("c1", "10", group="Grup A"),
                    make_chapter("c2", "10", group="Grup B")]
        with mock.patch.object(manga, "_resolve_titles",
                               async_return([make_title()])), \
                mock.patch.object(mangadex, "get_releases",
                                  async_return(releases)):
            await manga.read_cmd(make_update(message),
                                 make_context(["One", "Piece", "10"]))

        last = message.replies[-1]
        self.assertIn("Pilih rilisan chapter 10", last["text"])
        labels = [btn.text for row in last["reply_markup"].inline_keyboard
                  for btn in row]
        self.assertEqual(len(labels), 2)
        self.assertTrue(all(btn.callback_data.startswith("mg|g2|")
                            for row in last["reply_markup"].inline_keyboard
                            for btn in row))

    async def test_expired_callback_message(self):
        query = FakeCallbackQuery("mg|c|nosuchtoken|0")
        await manga.manga_callback(make_callback_update(query), mock.Mock())
        self.assertIn("kedaluwarsa", [e["text"] for e in query.edits][0])

    async def test_invalid_callback_data_ignored(self):
        query = FakeCallbackQuery("other|format")
        await manga.manga_callback(make_callback_update(query), mock.Mock())
        self.assertEqual(query.edits, [])

    async def test_duplicate_callback_press_is_ignored(self):
        """Tombol ditekan dua kali cepat tidak memicu request dobel."""
        title = make_title()
        token = manga._new_session(kind="chapters", title=title)
        query = FakeCallbackQuery(f"mg|c|{token}|0")
        calls = []

        async def fake_list(q, t, page):
            calls.append(t)

        # tekan kedua masih dalam pemrosesan tekanan pertama
        with mock.patch.object(manga, "_send_chapter_list_from_callback", fake_list), \
                mock.patch.object(manga, "_INFLIGHT",
                                  {f"111:{query.data}"}):
            await manga.manga_callback(make_callback_update(query), mock.Mock())

        self.assertEqual(calls, [])
        self.assertTrue(any("Masih diproses" in (a or "")
                            for a in query.answers))

    async def test_release_picker_callback_shows_groups(self):
        title = make_title()
        releases = [make_chapter("c1", "10", group="Grup A"),
                    make_chapter("c2", "10", group="Grup B")]
        token = manga._new_session(kind="chapters", title=title)
        query = FakeCallbackQuery(f"mg|g|{token}|10")

        async def fake_chapters(manga_id, lang):
            self.assertEqual(lang, "id")  # sesi tanpa lang = Bahasa Indonesia
            return [releases[0], make_chapter("c0", "9"), releases[1]], False

        with mock.patch.object(mangadex, "get_chapters", fake_chapters):
            await manga.manga_callback(make_callback_update(query), mock.Mock())

        last = query.edits[-1]
        self.assertIn("Pilih rilisan chapter 10", last["text"])
        self.assertIn("Bahasa Indonesia", last["text"])
        labels = [btn.text for row in last["reply_markup"].inline_keyboard
                  for btn in row]
        self.assertEqual(len(labels), 2)
        self.assertTrue(all(btn.callback_data.startswith("mg|g2|")
                            for row in last["reply_markup"].inline_keyboard
                            for btn in row))

    async def test_read_sends_pages_and_navigation(self):
        message = FakeMessage(text="pesan hasil")
        title = make_title()
        chapter = make_chapter("c1", "10", group="Grup A")
        token = manga._new_session(kind="releases", title=title, number="10",
                                   releases=[chapter])
        query = FakeCallbackQuery(f"mg|g2|{token}|0", message=message)

        async def fake_pages(chapter_id):
            return [f"https://cdn/{i}.jpg" for i in range(3)]

        sent_groups = []

        async def fake_batches(msg, images, caption):
            sent_groups.append((len(images), caption))
            return True

        async def fake_neighbours(t, number, chapter_id=None, lang="id"):
            return "9", "11"

        with mock.patch.object(mangadex, "get_pages", fake_pages), \
                mock.patch.object(mangadex, "download_image",
                                  async_return(b"\xff\xd8jpeg")), \
                mock.patch.object(manga, "_send_media_batches", fake_batches), \
                mock.patch.object(manga, "_neighbour_numbers", fake_neighbours):
            await manga.manga_callback(make_callback_update(query), mock.Mock())

        self.assertEqual(len(sent_groups), 1)
        count, caption = sent_groups[0]
        self.assertEqual(count, 3)
        self.assertIn("Bahasa: Indonesia (id)", caption)
        self.assertIn("Grup A", caption)
        self.assertIn("Source: MangaDex", caption)

        last = query.edits[-1]
        self.assertIn("selesai", last["text"])
        nav_labels = [btn.text for row in last["reply_markup"].inline_keyboard
                      for btn in row]
        self.assertIn("⬅️ Previous Chapter", nav_labels)
        self.assertIn("Next Chapter ➡️", nav_labels)
        self.assertIn("📋 Chapters", nav_labels)
        self.assertIn("🌐 Open Source", nav_labels)

    async def test_read_uses_external_link_when_no_pages(self):
        message = FakeMessage(text="pesan hasil")
        title = make_title()
        chapter = make_chapter("c1", "10",
                               external_url="https://example.com/baca")
        token = manga._new_session(kind="releases", title=title, number="10",
                                   releases=[chapter])
        query = FakeCallbackQuery(f"mg|g2|{token}|0", message=message)

        with mock.patch.object(mangadex, "get_pages", async_return(None)):
            await manga.manga_callback(make_callback_update(query), mock.Mock())

        self.assertIn("https://example.com/baca", query.edits[-1]["text"])

    async def test_read_falls_back_to_link_when_images_fail(self):
        message = FakeMessage(text="pesan hasil")
        title = make_title()
        chapter = make_chapter("c1", "10")
        token = manga._new_session(kind="releases", title=title, number="10",
                                   releases=[chapter])
        query = FakeCallbackQuery(f"mg|g2|{token}|0", message=message)

        with mock.patch.object(mangadex, "get_pages",
                               async_return(["https://cdn/1.jpg"])), \
                mock.patch.object(mangadex, "download_image",
                                  async_return(None)):
            await manga.manga_callback(make_callback_update(query), mock.Mock())

        last = query.edits[-1]["text"]
        self.assertIn("Gagal memuat halaman chapter", last)
        self.assertIn(chapter.url, last)

    async def test_neighbour_numbers(self):
        chapters = [make_chapter("c1", "9"), make_chapter("c2", "10"),
                    make_chapter("c3", "11")]

        async def fake_chapters(manga_id, lang):
            self.assertEqual(lang, "id")
            return chapters, False

        with mock.patch.object(mangadex, "get_chapters", fake_chapters):
            prev_number, next_number = await manga._neighbour_numbers(
                make_title(), None, chapter_id="c2")
        self.assertEqual((prev_number, next_number), ("9", "11"))


# ---------------------------------------------------------------------------
# Alur penawaran versi Inggris (hanya setelah konfirmasi)
# ---------------------------------------------------------------------------

class EnglishFallbackTests(unittest.IsolatedAsyncioTestCase):
    """Bahasa Inggris hanya tampil setelah tombol "Yes, English" ditekan."""

    async def test_offer_session_created_by_search_command(self):
        """/manhwa tanpa chapter Indonesia -> sesi penawaran, bukan hasil Inggris."""
        message = FakeMessage(text="/manhwa Solo Leveling")
        with mock.patch.object(manga, "_resolve_titles", async_return([])), \
                mock.patch.object(mangadex, "search_titles",
                                  async_return([make_title("x", "Solo Leveling",
                                                           category="manhwa")])):
            await manga.manhwa_cmd(make_update(message),
                                   make_context(["Solo", "Leveling"]))

        markup = message.edits[-1]["reply_markup"]
        callback_data = [btn.callback_data for row in markup.inline_keyboard
                         for btn in row]
        self.assertTrue(all(data.startswith("mg|yes|") or data.startswith("mg|no|")
                            for data in callback_data))
        token = callback_data[0].split("|")[2]
        session = manga.SESSIONS[token]
        self.assertEqual(session["kind"], "langoffer")
        self.assertEqual(session["query"], "Solo Leveling")
        self.assertEqual(session["category"], "manhwa")
        self.assertEqual(session["purpose"], "search")

    async def test_yes_english_searches_and_shows_english_results(self):
        """Tombol "Yes, English" -> pencarian Inggris dijalankan dan ditampilkan."""
        title = make_title("x", "Solo Leveling", category="manhwa")
        token = manga._new_session(kind="langoffer", query="Solo Leveling",
                                   category="manhwa", purpose="search",
                                   number=None)
        query = FakeCallbackQuery(f"mg|yes|{token}")
        seen = {}

        async def fake_find(query_text, category, lang=None, **kwargs):
            seen["query"] = query_text
            seen["category"] = category
            seen["lang"] = lang
            return [title]

        with mock.patch.object(mangadex, "find_titles_with_language", fake_find):
            await manga.manga_callback(make_callback_update(query), mock.Mock())

        self.assertEqual(seen, {"query": "Solo Leveling", "category": "manhwa",
                                "lang": "en"})
        last = query.edits[-1]
        self.assertIn("Solo Leveling", last["text"])
        self.assertIn("Chapter Inggris: 10", last["text"])
        labels = [btn.text for row in last["reply_markup"].inline_keyboard
                  for btn in row]
        self.assertIn("📖 View Chapters", labels)
        # sesi hasil Inggris tersimpan dengan bahasa Inggris
        session_token = last["reply_markup"].inline_keyboard[0][0].callback_data.split("|")[2]
        self.assertEqual(manga.SESSIONS[session_token]["lang"], "en")

    async def test_english_session_lists_english_chapters(self):
        """Dari sesi Inggris, daftar chapter memakai feed & label Bahasa Inggris."""
        title = make_title("x", "Solo Leveling", category="manhwa")
        token = manga._new_session(kind="search", query="Solo Leveling",
                                   category="manhwa", titles=[title],
                                   lang=mangadex.ENGLISH_LANG)
        chapters = [make_chapter("e1", "1", language="en", group="EN Group"),
                    make_chapter("e2", "2", language="en", group="EN Group")]
        query = FakeCallbackQuery(f"mg|c|{token}|0")
        seen = []

        async def fake_chapters(manga_id, lang):
            seen.append(lang)
            return chapters, False

        with mock.patch.object(mangadex, "get_chapters", fake_chapters):
            await manga.manga_callback(make_callback_update(query), mock.Mock())

        self.assertEqual(seen, ["en"])
        last = query.edits[-1]
        self.assertIn("Chapter Inggris — Solo Leveling", last["text"])
        self.assertIn("Bahasa: Inggris (en)", last["text"])
        self.assertIn("🇬🇧", last["text"])
        self.assertIn("Source: MangaDex", last["text"])
        # tombol rilisan memakai sesi berbahasa Inggris
        labels = [btn.text for row in last["reply_markup"].inline_keyboard
                  for btn in row]
        self.assertTrue(any(label.startswith("📖 Ch.") for label in labels))

    async def test_english_reading_navigation_uses_english_feed(self):
        """Navigasi baca dari sesi Inggris mencari rilisan di feed Inggris."""
        title = make_title("x", "Solo Leveling", category="manhwa")
        chapter = make_chapter("e11", "11", language="en", group="EN Group")
        token = manga._new_session(kind="reader", title=title,
                                   lang=mangadex.ENGLISH_LANG)
        query = FakeCallbackQuery(f"mg|r|{token}|11")
        seen = {}

        async def fake_releases(manga_id, number, lang):
            seen["lang"] = lang
            return [chapter]

        async def fake_read(callback_query, t, number, lang):
            seen["read_lang"] = lang

        with mock.patch.object(mangadex, "get_releases", fake_releases), \
                mock.patch.object(manga, "_send_pages_from_callback", fake_read):
            await manga.manga_callback(make_callback_update(query), mock.Mock())

        self.assertEqual(seen, {"lang": "en", "read_lang": "en"})

    async def test_english_read_offer_reads_confirmed_number(self):
        """Konfirmasi dari alur /read -> baca nomor itu di bahasa Inggris."""
        title = make_title("x", "Solo Leveling", category="manhwa")
        token = manga._new_session(kind="langoffer", query="Solo Leveling",
                                   category=None, purpose="read", number=5.0)
        query = FakeCallbackQuery(f"mg|yes|{token}")
        seen = {}

        async def fake_find(query_text, category, lang=None, **kwargs):
            self.assertEqual(lang, "en")
            return [title]

        async def fake_read(callback_query, t, number, lang):
            seen["number"] = number
            seen["lang"] = lang

        with mock.patch.object(mangadex, "find_titles_with_language", fake_find), \
                mock.patch.object(manga, "_start_read_from_callback", fake_read):
            await manga.manga_callback(make_callback_update(query), mock.Mock())

        self.assertEqual(seen, {"number": 5.0, "lang": "en"})

    async def test_no_cancel_ends_search_without_english(self):
        """"No, Cancel" -> pencarian batal; feed Inggris tidak pernah disentuh."""
        token = manga._new_session(kind="langoffer", query="Solo Leveling",
                                   category=None, purpose="search", number=None)
        query = FakeCallbackQuery(f"mg|no|{token}")
        called = []

        async def fake_find(*args, **kwargs):
            called.append(args)
            return []

        with mock.patch.object(mangadex, "find_titles_with_language", fake_find):
            await manga.manga_callback(make_callback_update(query), mock.Mock())

        self.assertEqual(called, [])
        self.assertIn("dibatalkan", query.edits[-1]["text"])
        self.assertIsNone(manga.SESSIONS.get(token))

    async def test_offer_consumed_once_then_expired(self):
        """Sesi penawaran sekali pakai: tekan ulang -> kedaluwarsa, bukan hasil."""
        token = manga._new_session(kind="langoffer", query="Solo",
                                   category=None, purpose="search", number=None)
        query = FakeCallbackQuery(f"mg|yes|{token}")
        with mock.patch.object(mangadex, "find_titles_with_language",
                               async_return([])):
            await manga.manga_callback(make_callback_update(query), mock.Mock())
        self.assertIsNone(manga.SESSIONS.get(token))

        second_press = FakeCallbackQuery(f"mg|yes|{token}")
        await manga.manga_callback(make_callback_update(second_press), mock.Mock())
        self.assertIn("kedaluwarsa", second_press.edits[0]["text"])

    async def test_expired_offer_callback_message(self):
        query = FakeCallbackQuery("mg|yes|expiredtoken|")
        await manga.manga_callback(make_callback_update(query), mock.Mock())
        self.assertIn("kedaluwarsa", query.edits[0]["text"])

    async def test_duplicate_offer_press_is_ignored(self):
        token = manga._new_session(kind="langoffer", query="Solo",
                                   category=None, purpose="search", number=None)
        query = FakeCallbackQuery(f"mg|yes|{token}")
        with mock.patch.object(manga, "_INFLIGHT", {f"111:{query.data}"}):
            await manga.manga_callback(make_callback_update(query), mock.Mock())
        self.assertEqual(query.edits, [])
        self.assertTrue(any("Masih diproses" in (a or "")
                            for a in query.answers))

    async def test_confirm_english_but_none_available(self):
        """Konfirmasi tapi tidak ada rilisan Inggris -> pesan jelas + kredit."""
        token = manga._new_session(kind="langoffer", query="Solo",
                                   category=None, purpose="search", number=None)
        query = FakeCallbackQuery(f"mg|yes|{token}")
        with mock.patch.object(mangadex, "find_titles_with_language",
                               async_return([])):
            await manga.manga_callback(make_callback_update(query), mock.Mock())

        text = query.edits[-1]["text"]
        self.assertIn("Tidak ada chapter Bahasa Inggris", text)
        self.assertIn("Source: MangaDex", text)

    async def test_indonesian_results_never_show_offer(self):
        """Judul dengan chapter Indonesia tetap tanpa tombol penawaran Inggris."""
        message = FakeMessage(text="/manga One Piece")
        with mock.patch.object(manga, "_resolve_titles",
                               async_return([make_title()])):
            await manga.manga_cmd(make_update(message),
                                  make_context(["One", "Piece"]))

        photos = [r for r in message.replies if "photo" in r]
        labels = [btn.text for row in photos[0]["reply_markup"].inline_keyboard
                  for btn in row]
        self.assertNotIn("Yes, English", labels)
        self.assertNotIn("No, Cancel", labels)


# ---------------------------------------------------------------------------
# Info, /mangafeature, routing, /feature
# ---------------------------------------------------------------------------

class InfoAndFeatureTests(unittest.IsolatedAsyncioTestCase):
    async def test_mangainfo_shows_details_and_source(self):
        message = FakeMessage(text="/mangainfo One Piece")
        title = make_title()
        with mock.patch.object(mangadex, "search_titles", async_return([title])), \
                mock.patch.object(mangadex, "language_stats",
                                  async_return((1, "10"))):
            await manga.mangainfo_cmd(make_update(message),
                                      make_context(["One", "Piece"]))

        photos = [r for r in message.replies if "photo" in r]
        self.assertEqual(len(photos), 1, "cover harus dikirim sebagai foto")
        self.assertEqual(photos[0]["photo"], "https://covers/cover.jpg")
        self.assertEqual(photos[0].get("parse_mode"), "HTML")

        text = photos[0]["caption"]
        # format kartu sesuai contoh: judul -> info dasar -> sinopsis -> dst.
        self.assertIn("📚 One Piece", text)
        self.assertIn("Category: Manga", text)
        self.assertIn("Status: Ongoing", text)
        self.assertIn("Author: Oda", text)
        self.assertIn("Alt Titles: OP", text)
        self.assertIn("Genres\nAction", text)
        self.assertIn("Latest Chapter: Chapter 10", text)
        self.assertIn("Available Language: Indonesian / English", text)
        self.assertIn("Source: MangaDex", text)
        self.assertIn("https://mangadex.org/", text)
        self.assertIn("<blockquote expandable>", text)
        self.assertIn("</blockquote>", text)
        self.assertLess(text.index("Category:"), text.index("Synopsis"))
        self.assertLess(text.index("Synopsis"), text.index("Genres"))
        self.assertLess(text.index("Genres"), text.index("Latest Chapter"))
        # wajib muat sebagai caption foto
        self.assertLessEqual(len(text), manga.CAPTION_LIMIT)
        labels = [btn.text for row in photos[0]["reply_markup"].inline_keyboard
                  for btn in row]
        self.assertIn("📖 Read Chapters", labels)
        self.assertIn("🌐 Open Source", labels)

    async def test_mangainfo_without_cover_falls_back_to_text(self):
        message = FakeMessage(text="/mangainfo One Piece")
        title = make_title(cover_url=None)
        with mock.patch.object(mangadex, "search_titles", async_return([title])), \
                mock.patch.object(mangadex, "language_stats",
                                  async_return((1, "10"))):
            await manga.mangainfo_cmd(make_update(message),
                                      make_context(["One", "Piece"]))

        self.assertFalse([r for r in message.replies if "photo" in r])
        text = last_reply_body(message)
        self.assertIn("📚 One Piece", text)
        self.assertIn("Latest Chapter: Chapter 10", text)
        self.assertIn("Available Language: Indonesian / English", text)
        self.assertIn("Source: MangaDex", text)

    async def test_info_caption_stays_within_limit(self):
        """Judul/genre/sinopsis panjang tetap muat & tag tidak terpotong."""
        title = make_title(
            title="<b>Judul & Kawan</b>",
            synopsis=("Paragraf pertama yang panjang sekali. " * 60).strip()
                      + "\n\n\n\n" + "Paragraf kedua.",
            alt_titles=[f"Judul alternatif nomor {i}" for i in range(10)],
            genres=[f"Genre nomor {i}" for i in range(15)],
        )
        text = manga._info_text(title, "10", ["id", "en"])

        self.assertLessEqual(len(text), manga.CAPTION_LIMIT)
        self.assertTrue(text.endswith("Source: MangaDex"))
        self.assertNotIn("&lt;blockquote", text)  # tag tidak ikut ter-escape
        if "<blockquote" in text:
            self.assertIn("</blockquote>", text)
        self.assertIn("&lt;b&gt;Judul &amp; Kawan&lt;/b&gt;", text)
        self.assertIn("+9 more", text)   # 15 genre -> 6 ditampilkan
        self.assertIn("+7 more", text)   # 10 judul alternatif -> 3 ditampilkan
        self.assertIn("Available Language: Indonesian / English", text)

    async def test_mangainfo_notes_missing_indonesian(self):
        message = FakeMessage(text="/mangainfo Zzz")
        title = make_title(id_count=None, latest=None)
        with mock.patch.object(mangadex, "search_titles", async_return([title])), \
                mock.patch.object(mangadex, "language_stats",
                                  async_return((0, None))):
            await manga.mangainfo_cmd(make_update(message), make_context(["Zzz"]))

        text = last_reply_body(message)
        self.assertIn("Latest Chapter: N/A", text)
        self.assertIn("Available Language: N/A", text)

    async def test_info_button_updates_cover_caption(self):
        """Tombol 'Manga Info' di hasil pencarian foto -> caption diganti info."""
        message = FakeMessage(text="hasil pencarian")
        message.photo = ["photo-file"]      # hasil pencarian berupa cover
        title = make_title()
        token = manga._new_session(kind="search", titles=[title],
                                   category="manga")
        query = FakeCallbackQuery(f"mg|i|{token}|0", message=message)
        with mock.patch.object(mangadex, "language_stats",
                               async_return((10, "10"))):
            await manga.manga_callback(make_callback_update(query), mock.Mock())

        captions = [e for e in query.edits if "caption" in e]
        self.assertEqual(len(captions), 1, "info harus mengedit caption foto")
        self.assertIn("One Piece", captions[0]["caption"])
        self.assertLessEqual(len(captions[0]["caption"]), manga.CAPTION_LIMIT)

    async def test_info_from_picker_sends_cover_photo(self):
        """Dari pesan pemilih judul (teks) -> cover + info dikirim sebagai foto."""
        message = FakeMessage(text="Silakan pilih salah satu hasil pencarian.")
        title = make_title()
        token = manga._new_session(kind="resolve", purpose="info",
                                   query="One Piece", titles=[title])
        query = FakeCallbackQuery(f"mg|p|{token}|0", message=message)
        with mock.patch.object(mangadex, "language_stats",
                               async_return((10, "10"))):
            await manga.manga_callback(make_callback_update(query), mock.Mock())

        photos = [r for r in message.replies if "photo" in r]
        self.assertEqual(len(photos), 1)
        self.assertEqual(photos[0]["photo"], "https://covers/cover.jpg")
        self.assertIn("One Piece", photos[0]["caption"])
        self.assertTrue(message.deleted, "pesan pemilih lama harus dibersihkan")

    async def test_mangafeature_lists_all_commands(self):
        message = FakeMessage(text="/mangafeature")
        await manga.mangafeature_cmd(make_update(message), mock.Mock())
        text = message.replies[0]["text"]
        for command in ("/manga", "/manhwa", "/comic", "/chapter", "/read",
                        "/mangainfo", "/mangafeature"):
            self.assertIn(command, text)
        self.assertIn("Source: MangaDex", text)
        self.assertEqual(message.replies[0].get("parse_mode"), "HTML")


class RoutingTests(unittest.TestCase):
    """Handler manga terdaftar di main() tanpa mengganggu fitur lama."""

    def _handlers(self):
        from telegram.ext import Application

        captured = {}

        def fake_run(self, *args, **kwargs):
            captured["handlers"] = self.handlers

        with mock.patch.object(Application, "run_polling", fake_run):
            bot.main()
        return captured["handlers"][0]

    def test_manga_handlers_registered(self):
        callbacks = [getattr(handler, "callback", None).__name__
                     for handler in self._handlers()]
        for expected in ("manga_cmd", "manhwa_cmd", "comic_cmd", "chapter_cmd",
                         "read_cmd", "mangainfo_cmd", "mangafeature_cmd",
                         "manga_callback"):
            self.assertIn(expected, callbacks)
        self.assertLess(callbacks.index("manga_cmd"),
                        callbacks.index("handle_message"))

    def test_old_handlers_still_registered(self):
        callbacks = [getattr(handler, "callback", None).__name__
                     for handler in self._handlers()]
        for expected in ("start", "anime", "restart_bot", "pdf_command",
                         "word_command", "feature"):
            self.assertIn(expected, callbacks)

    def test_callback_query_handler_matches_manga_pattern(self):
        from datetime import datetime, timezone

        from telegram import Bot, CallbackQuery, Chat, Message, Update, User

        bot_api = Bot(token="TEST_TOKEN_NOT_A_REAL_BOT")
        bot_api._bot_user = User(id=1, is_bot=True, first_name="Test",
                                 username="test_bot")
        message = Message(
            message_id=1, date=datetime.now(timezone.utc),
            chat=Chat(id=1, type="private"),
            from_user=User(id=1, first_name="U", is_bot=False),
            text="x",
        )
        message.set_bot(bot_api)
        update = Update(update_id=1, callback_query=CallbackQuery(
            id="1", from_user=User(id=1, first_name="U", is_bot=False),
            chat_instance="ci", data="mg|c|token|0", message=message))

        handler = next(h for h in self._handlers()
                       if getattr(h, "callback", None).__name__ == "manga_callback")
        self.assertTrue(handler.check_update(update))

        other = Update(update_id=2, callback_query=CallbackQuery(
            id="2", from_user=User(id=1, first_name="U", is_bot=False),
            chat_instance="ci", data="random-data", message=message))
        self.assertFalse(handler.check_update(other))

    def test_feature_list_contains_manga_commands(self):
        text = bot._build_feature_text()
        for token in ("/manga", "/manhwa", "/comic", "/chapter", "/read",
                      "/mangainfo", "/mangafeature"):
            self.assertIn(token, text, f"command hilang dari /feature: {token}")
        self.assertIn("📚 Manga dan Manhwa", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
