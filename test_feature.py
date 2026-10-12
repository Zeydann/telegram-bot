"""Tests for the /feature command.

Run with:

    venv/bin/python test_feature.py
"""

import unittest
import xml.etree.ElementTree as ElementTree
from unittest import mock

import bot


class FakeMessage:
    def __init__(self, text=None):
        self.text = text
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append({"text": text, **kwargs})
        return self


class FakeUpdate:
    def __init__(self, message, user_id=111):
        self.message = message
        self.effective_user = type("User", (), {"id": user_id})()


class FeatureMessageTests(unittest.TestCase):
    def test_lists_only_implemented_features(self):
        """Semua handler yang terdaftar di main() harus muncul, tanpa fitur karangan."""
        text = bot._build_feature_text()
        for token in (
            "TikTok Downloader",
            "Instagram Downloader",
            "Pinterest Downloader",
            "Anime Search",
            "PDF to Word",
            "Word to PDF",
            "Feature List",
            "Welcome",
            "Restart",
            "/anime",
            "/pdf",
            "/word",
            "/feature",
            "/start",
            "/restart",
        ):
            self.assertIn(token, text, f"fitur/command hilang: {token}")

    def test_every_category_has_emoji_marker(self):
        for category, emoji, _items in bot.FEATURES:
            self.assertTrue(emoji, f"kategori {category} tanpa emoji")
            self.assertIn(f"{emoji} {category}", bot._build_feature_text())

    def test_html_is_well_formed(self):
        """Tag seimbang & karakter spesial ter-escape (aman untuk parse_mode HTML)."""
        text = bot._build_feature_text()
        ElementTree.fromstring(f"<root>{text}</root>")  # melempar error bila tag/r&salah

    def test_special_characters_are_escaped(self):
        poisoned = (
            ("Kategori <b>&</b>", "🧪", (("Nama <i>x</i>", "deskripsi & 'kutip' <tag>", "/cmd"),),),
        )
        with mock.patch.object(bot, "FEATURES", poisoned):
            text = bot._build_feature_text()

        self.assertIn("Kategori &lt;b&gt;&amp;&lt;/b&gt;", text)
        self.assertIn("Nama &lt;i&gt;x&lt;/i&gt;", text)
        self.assertIn("&amp; &#x27;kutip&#x27; &lt;tag&gt;", text)
        ElementTree.fromstring(f"<root>{text}</root>")

    def test_message_is_short(self):
        text = bot._build_feature_text()
        self.assertLess(len(text), 2000, "pesan /feature terlalu panjang")
        self.assertLess(len(text), 4096, "melebihi limit pesan Telegram")


class FeatureHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_replies_with_html_parse_mode(self):
        message = FakeMessage(text="/feature")
        await bot.feature(FakeUpdate(message), None)

        self.assertEqual(len(message.replies), 1)
        reply = message.replies[0]
        self.assertEqual(reply.get("parse_mode"), "HTML")
        self.assertIn("<b>✨ FITUR BOT</b>", reply["text"])
        self.assertIn("/pdf", reply["text"])
        # pesan tanpa link eksternal sehingga preview tidak perlu dibuka
        self.assertTrue(reply.get("disable_web_page_preview"))

    async def test_old_commands_still_respond(self):
        # /start tetap membalas (tanpa jaringan)
        start_message = FakeMessage(text="/start")
        await bot.start(FakeUpdate(start_message), None)
        self.assertIn("TikTok", start_message.replies[0]["text"])

        # /anime tanpa argumen tetap membalas pemakaian (tanpa jaringan)
        anime_message = FakeMessage(text="/anime")
        await bot.anime(FakeUpdate(anime_message), mock.Mock(args=[]))
        self.assertIn("/anime", anime_message.replies[0]["text"])


class FeatureRoutingTests(unittest.TestCase):
    TOKEN = "TEST_TOKEN_NOT_A_REAL_BOT"

    def setUp(self):
        from datetime import datetime, timezone

        from telegram import Bot, Chat, Message, MessageEntity, Update, User

        self.bot_api = Bot(token=self.TOKEN)
        self.bot_api._bot_user = User(id=1, is_bot=True, first_name="Test", username="test_bot")
        message = Message(
            message_id=1,
            date=datetime.now(timezone.utc),
            chat=Chat(id=1, type="private"),
            from_user=User(id=1, first_name="U", is_bot=False),
            text="/feature",
            entities=[MessageEntity(type="bot_command", offset=0, length=8)],
        )
        message.set_bot(self.bot_api)
        self.update = Update(update_id=1, message=message)

    def test_command_handler_matches_feature(self):
        from telegram.ext import CommandHandler

        handler = CommandHandler("feature", bot.feature)
        self.assertTrue(handler.check_update(self.update))

    def test_feature_registered_before_generic_text_handler(self):
        from telegram.ext import Application

        captured = {}

        def fake_run(self, *args, **kwargs):
            captured["handlers"] = self.handlers

        with mock.patch.object(Application, "run_polling", fake_run):
            bot.main()

        callbacks = [getattr(handler, "callback", None).__name__ for handler in captured["handlers"][0]]
        # command lama tetap terdaftar
        for expected in ("start", "anime", "restart_bot", "pdf_command", "word_command", "feature"):
            self.assertIn(expected, callbacks)
        # /feature tidak duplikat dan berada sebelum handler teks biasa
        self.assertEqual(callbacks.count("feature"), 1)
        self.assertLess(callbacks.index("feature"), callbacks.index("handle_message"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
