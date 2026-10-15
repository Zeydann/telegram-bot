"""Tests for the Server Monitor feature (/status, /health, /logs, /stats).

Run with:

    venv/bin/python test_monitor.py

Everything runs offline: external HTTP checks are mocked, the log and
statistics files used by the tests live in temporary directories, and the
suites never enable recording of the production statistics file.
"""

import asyncio
import json
import logging
import os
import stat
import tempfile
import time
import unittest
import xml.etree.ElementTree as ElementTree
from types import SimpleNamespace
from unittest import mock

import bot
import config
import logviewer
import monitor
import stats
from utils import WIB, format_duration, human_bytes, now_wib, wib_text


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeMessage:
    def __init__(self, text=None):
        self.text = text
        self.caption = None
        self.document = None
        self.reply_to_message = None
        self.replies = []
        self.documents = []
        self.edits = []
        self.deletes = 0

    async def reply_text(self, text, **kwargs):
        self.replies.append({"text": text, **kwargs})
        return self

    async def edit_text(self, text, **kwargs):
        self.edits.append(text)
        return self

    async def delete(self):
        self.deletes += 1
        return True

    async def reply_document(self, document=None, filename=None, caption=None, **kwargs):
        data = document.read() if hasattr(document, "read") else document
        self.documents.append({"data": data, "filename": filename,
                               "caption": caption, **kwargs})
        return self


class FakeUpdate:
    def __init__(self, user_id=111):
        self.message = FakeMessage()
        self.effective_user = SimpleNamespace(id=user_id)


class FakeContext:
    def __init__(self, args=None, bot=None):
        self.args = args or []
        self.bot = bot


def assert_html_valid(testcase, text):
    """Lempar error bila tag tidak seimbang / karakter tak ter-escape."""
    ElementTree.fromstring(f"<root>{text}</root>")


def write_log_lines(path, lines):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def sample_measurements(cpu=12.5, mem_percent=40.0, disk_percent=55.0):
    """Trio pengukuran palsu (CPU, RAM, disk) + uptime untuk membangun pesan."""
    total = 8 * 1024 ** 3
    available = int(total * (100 - mem_percent) / 100)
    disk_total = 100 * 1024 ** 3
    disk_used = int(disk_total * disk_percent / 100)
    return (
        cpu,
        {"total": total, "available": available, "used": total - available},
        {"total": disk_total, "used": disk_used, "free": disk_total - disk_used},
        90061.0,
    )


# ---------------------------------------------------------------------------
# /status
# ---------------------------------------------------------------------------

class StatusTextTests(unittest.TestCase):
    def test_contains_every_required_section(self):
        text = monitor._build_status_text(*sample_measurements())
        for token in (
            "STATUS SERVER", "ONLINE", "Bot uptime", "Server uptime",
            "CPU", "RAM", "Disk", "NORMAL", "Diperiksa",
        ):
            self.assertIn(token, text, f"bagian hilang: {token}")
        assert_html_valid(self, text)

    def test_indicators_follow_thresholds(self):
        # CPU & RAM di atas CRITICAL (90), disk 85% di rentang WARNING (80)
        text = monitor._build_status_text(
            *sample_measurements(cpu=95.0, mem_percent=95.0, disk_percent=85.0))
        self.assertIn("[CRITICAL]", text)
        self.assertIn("[WARNING]", text)

    def test_memory_and_disk_shows_absolute_and_percent(self):
        text = monitor._build_status_text(*sample_measurements())
        self.assertIn("RAM:", text)
        self.assertIn("tersedia", text)
        self.assertRegex(text, r"\d+\.\d%")  # persentase tampil
        assert_html_valid(self, text)

    def test_missing_measurements_reported_as_na(self):
        text = monitor._build_status_text(None, None, None, None)
        self.assertIn("n/a", text)
        self.assertIn("tidak tersedia", text)
        assert_html_valid(self, text)

    def test_usage_level_boundaries(self):
        level = monitor.usage_level
        self.assertEqual(level(0, 70, 90), "NORMAL")
        self.assertEqual(level(69.9, 70, 90), "NORMAL")
        self.assertEqual(level(70, 70, 90), "WARNING")   # batas warning tereksklusif
        self.assertEqual(level(89.9, 70, 90), "WARNING")
        self.assertEqual(level(90, 70, 90), "CRITICAL")  # batas critical tereksklusif
        self.assertEqual(level(100, 70, 90), "CRITICAL")
        self.assertEqual(level(None, 70, 90), "NORMAL")

    def test_no_internal_paths_leaked_to_normal_users(self):
        """/status untuk publik: tidak membocorkan path filesystem proyek."""
        text = monitor._build_status_text(*sample_measurements())
        self.assertNotIn(os.path.abspath(os.path.dirname(__file__)), text)
        self.assertNotIn("monitor.", text)


class StatusCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_replies_with_html(self):
        measurements = sample_measurements()
        with mock.patch.object(monitor, "sample_cpu", return_value=measurements[0]), \
             mock.patch.object(monitor, "read_memory", return_value=measurements[1]), \
             mock.patch.object(monitor, "read_disk", return_value=measurements[2]), \
             mock.patch.object(monitor, "read_uptime", return_value=measurements[3]):
            update = FakeUpdate()
            await monitor.status_cmd(update, None)

        self.assertEqual(len(update.message.replies), 1)
        reply = update.message.replies[0]
        self.assertEqual(reply.get("parse_mode"), "HTML")
        self.assertIn("STATUS SERVER", reply["text"])
        assert_html_valid(self, reply["text"])

    async def test_event_loop_stays_responsive_during_cpu_sampling(self):
        """Sampling CPU (0,25 dtk) berjalan di thread — loop bot tidak tersendat."""
        gaps = []
        stop = asyncio.Event()

        async def ticker():
            last = time.monotonic()
            while not stop.is_set():
                await asyncio.sleep(0.02)
                now = time.monotonic()
                gaps.append(now - last)
                last = now

        task = asyncio.create_task(ticker())
        try:
            await monitor.status_cmd(FakeUpdate(), None)  # sampling CPU asli
        finally:
            stop.set()
            await task

        self.assertTrue(gaps, "ticker tidak pernah berjalan")
        # Bila sampling memblokir loop, jeda akan mendekati 0,25 detik.
        self.assertLess(max(gaps), 0.2,
                        f"event loop tersendat saat sampling CPU (jeda {max(gaps):.3f}s)")


class RealMeasurementTests(unittest.TestCase):
    """Pengukuran asli di Linux — membuktikan angka bukan nilai statis."""

    def test_cpu_sample_is_a_percentage(self):
        value = monitor.sample_cpu(0.05)
        if value is None:
            self.skipTest("/proc/stat tidak tersedia di sistem ini")
        self.assertGreaterEqual(value, 0.0)
        self.assertLessEqual(value, 100.0)

    def test_memory_reading_is_consistent(self):
        memory = monitor.read_memory()
        if memory is None:
            self.skipTest("/proc/meminfo tidak tersedia di sistem ini")
        self.assertGreater(memory["total"], 0)
        self.assertGreaterEqual(memory["available"], 0)
        self.assertLessEqual(memory["used"], memory["total"])

    def test_uptime_and_disk(self):
        uptime = monitor.read_uptime()
        if uptime is None:
            self.skipTest("/proc/uptime tidak tersedia di sistem ini")
        self.assertGreater(uptime, 0.0)

        disk = monitor.read_disk(os.path.dirname(os.path.abspath(__file__)))
        self.assertIsNotNone(disk)
        self.assertGreater(disk["total"], 0)


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------

class HealthCheckTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    async def test_run_checks_covers_every_required_area(self):
        async def fake_http(name, url, headers=None):
            return monitor.Check(name, "OK", "mock", external=True)

        async def fake_telegram(context):
            return monitor.Check("Telegram API", "OK", "mock", external=True)

        async def fake_ilovepdf():
            return monitor.Check("iLovePDF API", "SKIP", "belum dikonfigurasi")

        with mock.patch.object(monitor, "_check_http", new=fake_http), \
             mock.patch.object(monitor, "_check_telegram", new=fake_telegram), \
             mock.patch.object(monitor, "_check_ilovepdf", new=fake_ilovepdf), \
             mock.patch.object(monitor, "DOWNLOAD_DIR",
                               os.path.join(self.tmp.name, "download")), \
             mock.patch.object(monitor, "STATS_FILE",
                               os.path.join(self.tmp.name, "data", "stats.json")), \
             mock.patch.object(monitor, "LOG_FILE",
                               os.path.join(self.tmp.name, "logs", "bot.log")):
            checks = await monitor.run_checks(FakeContext())

        names = [check.name for check in checks]
        self.assertIn("Proses bot", names)
        self.assertIn("Direktori unduhan", names)
        self.assertIn("Ruang disk (unduhan)", names)
        self.assertIn("Ruang disk (proyek)", names)
        self.assertIn("Telegram API", names)
        self.assertIn("MangaDex API", names)
        self.assertIn("MyAnimeList", names)
        # project ini TIDAK memakai database → tidak ada cek database yang gagal
        self.assertFalse(any("database" in name.lower() for name in names))
        for check in checks:
            self.assertIn(check.status, ("OK", "WARNING", "FAILED", "SKIP"))
            # direktori sementara dibuat & terbukti bisa ditulis
            if check.name.startswith("Direktori"):
                self.assertEqual(check.status, "OK", check.detail)

    async def test_telegram_failure_is_tagged_external(self):
        class BoomBot:
            async def get_me(self):
                raise RuntimeError("nope")

        check = await monitor._check_telegram(FakeContext(bot=BoomBot()))
        self.assertEqual(check.status, "FAILED")
        self.assertTrue(check.external)
        self.assertIn("RuntimeError", check.detail)

    async def test_telegram_timeout_is_bounded(self):
        class SlowBot:
            async def get_me(self):
                await asyncio.sleep(5)
                return SimpleNamespace(username="x")

        with mock.patch.object(monitor, "HEALTH_TIMEOUT", 0.05):
            check = await monitor._check_telegram(FakeContext(bot=SlowBot()))
        self.assertEqual(check.status, "FAILED")
        self.assertTrue(check.external)
        self.assertIn("tidak merespons", check.detail)

    async def test_http_check_timeout_is_external_failure(self):
        import httpx

        class TimeoutClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, url):
                raise httpx.TimeoutException("slow")

        with mock.patch.object(monitor.httpx, "AsyncClient", TimeoutClient):
            check = await monitor._check_http("Uji", "https://example.invalid/")
        self.assertEqual(check.status, "FAILED")
        self.assertTrue(check.external)

    async def test_ilovepdf_skipped_when_not_configured(self):
        with mock.patch.object(monitor, "ILOVEPDF_PUBLIC_KEY", ""), \
             mock.patch.object(monitor, "ILOVEPDF_SECRET_KEY", ""):
            check = await monitor._check_ilovepdf()
        self.assertEqual(check.status, "SKIP")
        self.assertFalse(check.external)

    async def test_ilovepdf_checked_when_configured(self):
        seen = {}

        async def fake_http(name, url, headers=None):
            seen["name"] = name
            return monitor.Check(name, "OK", "mock", external=True)

        with mock.patch.object(monitor, "ILOVEPDF_PUBLIC_KEY", "pub"), \
             mock.patch.object(monitor, "ILOVEPDF_SECRET_KEY", "sec"), \
             mock.patch.object(monitor, "_check_http", new=fake_http):
            check = await monitor._check_ilovepdf()
        self.assertEqual(seen.get("name"), "iLovePDF API")
        self.assertEqual(check.status, "OK")


class HealthCheckUnitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_writable_dir_ok_and_created(self):
        path = os.path.join(self.tmp.name, "baru", "sudah")
        check = monitor._check_writable_dir("Direktori uji", path)
        self.assertEqual(check.status, "OK")
        self.assertTrue(os.path.isdir(path))

    def test_path_behind_a_file_fails(self):
        blocker = os.path.join(self.tmp.name, "bukan_dir")
        with open(blocker, "w") as fh:
            fh.write("x")
        check = monitor._check_writable_dir("Direktori uji", os.path.join(blocker, "sub"))
        self.assertEqual(check.status, "FAILED")

    def test_disk_thresholds(self):
        gib = 1024 ** 3
        mb = 1024 ** 2

        def usage(free):
            return SimpleNamespace(total=100 * gib, used=100 * gib - free, free=free)

        with mock.patch.object(monitor.shutil, "disk_usage", side_effect=lambda p: usage(10 * gib)):
            self.assertEqual(monitor._check_disk_space("D", self.tmp.name).status, "OK")
        with mock.patch.object(monitor.shutil, "disk_usage", side_effect=lambda p: usage(200 * mb)):
            self.assertEqual(monitor._check_disk_space("D", self.tmp.name).status, "WARNING")
        with mock.patch.object(monitor.shutil, "disk_usage", side_effect=lambda p: usage(50 * mb)):
            self.assertEqual(monitor._check_disk_space("D", self.tmp.name).status, "FAILED")
        with mock.patch.object(monitor.shutil, "disk_usage", side_effect=OSError("nope")):
            self.assertEqual(monitor._check_disk_space("D", self.tmp.name).status, "FAILED")


class HealthMessageTests(unittest.IsolatedAsyncioTestCase):
    def _checks(self):
        return [
            monitor.Check("Proses bot", "OK", "berjalan"),
            monitor.Check("Direktori unduhan", "OK", "/tmp/x dapat ditulis"),
            monitor.Check("Ruang disk (proyek)", "WARNING", "menipis"),
            monitor.Check("MangaDex API", "FAILED", "timeout 5 detik", external=True),
            monitor.Check("iLovePDF API", "SKIP", "belum dikonfigurasi"),
        ]

    def test_message_structure_and_tags(self):
        text = monitor._build_health_text(self._checks())
        for token in ("HEALTH CHECK", "Status:", "Pemeriksaan:",
                      "OK", "WARNING", "FAILED", "SKIP"):
            self.assertIn(token, text)
        # kegagalan eksternal vs internal harus dibedakan secara eksplisit
        self.assertIn("[layanan eksternal]", text)
        self.assertIn("[internal]", text)
        assert_html_valid(self, text)
        self.assertLess(len(text), 4096)

    def test_overall_status_distinguishes_internal_and_external(self):
        internal_failed = [monitor.Check("X", "FAILED", "disk penuh")]
        external_failed = [monitor.Check("Y", "FAILED", "timeout", external=True)]
        healthy = [monitor.Check("X", "OK", "aman")]

        self.assertIn("ADA MASALAH", monitor._build_health_text(internal_failed))
        self.assertIn("MASALAH LAYANAN EKSTERNAL",
                      monitor._build_health_text(external_failed))
        self.assertIn("SEHAT", monitor._build_health_text(healthy))

    def test_detail_is_escaped(self):
        checks = [monitor.Check("X<b>", "OK", "isi <script>&amp;")]
        text = monitor._build_health_text(checks)
        assert_html_valid(self, text)
        self.assertNotIn("<script>", text)

    async def test_health_command_replies_html(self):
        async def fake_run_checks(context):
            return [monitor.Check("Proses bot", "OK", "berjalan")]

        with mock.patch.object(monitor, "run_checks", new=fake_run_checks):
            update = FakeUpdate()
            await monitor.health_cmd(update, FakeContext())

        reply = update.message.replies[0]
        self.assertEqual(reply.get("parse_mode"), "HTML")
        self.assertIn("HEALTH CHECK", reply["text"])


# ---------------------------------------------------------------------------
# /logs
# ---------------------------------------------------------------------------

class LogsAuthTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_admin_rejected_before_any_file_access(self):
        """Wajib: file log tidak pernah disentuh untuk non-admin."""
        update = FakeUpdate(user_id=999_999_999_999)
        with mock.patch.object(logviewer, "is_admin", return_value=False), \
             mock.patch.object(logviewer, "tail_lines",
                               side_effect=AssertionError("file log dibaca non-admin!")) \
                as spy:
            await logviewer.logs_cmd(update, FakeContext(args=[]))

        self.assertEqual(len(update.message.replies), 1)
        self.assertIn("tidak punya akses", update.message.replies[0]["text"])
        spy.assert_not_called()

    async def test_non_admin_rejected_with_real_config(self):
        """Tanpa mock otorisasi: ID acak tidak pernah ada di ADMIN_IDS."""
        self.assertFalse(config.is_admin(999_999_999_999))
        update = FakeUpdate(user_id=999_999_999_999)
        with mock.patch.object(logviewer, "tail_lines",
                               side_effect=AssertionError("file log dibaca non-admin!")):
            await logviewer.logs_cmd(update, FakeContext(args=[]))
        self.assertIn("tidak punya akses", update.message.replies[0]["text"])

    async def test_configured_admin_id_is_accepted(self):
        if not config.ADMIN_IDS:
            self.skipTest("tidak ada ADMIN_ID/ADMIN_IDS di .env")
        admin_id = next(iter(config.ADMIN_IDS))
        self.assertTrue(config.is_admin(admin_id))

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        log_path = os.path.join(tmp.name, "bot.log")
        write_log_lines(log_path, [
            "2026-01-01 00:00:00 - tiktok_bot - INFO - baris admin"
        ])
        with mock.patch.object(logviewer, "LOG_FILE", log_path):
            update = FakeUpdate(user_id=admin_id)
            await logviewer.logs_cmd(update, FakeContext(args=[]))

        self.assertEqual(len(update.message.replies), 1)
        self.assertIn("baris admin", update.message.replies[0]["text"])

    def test_admin_matching_is_by_numeric_id(self):
        with mock.patch.object(config, "ADMIN_IDS", frozenset({42})):
            self.assertTrue(config.is_admin(42))
            self.assertTrue(config.is_admin("42"))   # string dari parser juga cocok
            self.assertFalse(config.is_admin(43))
            self.assertFalse(config.is_admin("bukan-angka"))
            self.assertFalse(config.is_admin(None))


class LogsCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log_path = os.path.join(self.tmp.name, "bot.log")
        self._admin = mock.patch.object(logviewer, "is_admin", return_value=True)
        self._admin.start()
        self.addCleanup(self._admin.stop)
        self._file = mock.patch.object(logviewer, "LOG_FILE", self.log_path)
        self._file.start()
        self.addCleanup(self._file.stop)

    def _sample_lines(self, count=20):
        lines = []
        for i in range(1, count + 1):
            level = "ERROR" if i % 5 == 0 else ("WARNING" if i % 3 == 0 else "INFO")
            lines.append(f"2026-01-01 00:00:{i:02d} - tiktok_bot - {level} - pesan {i}")
        return lines

    async def test_returns_tail_as_html(self):
        write_log_lines(self.log_path, self._sample_lines())
        update = FakeUpdate()
        await logviewer.logs_cmd(update, FakeContext(args=[]))

        reply = update.message.replies[0]
        self.assertEqual(reply.get("parse_mode"), "HTML")
        self.assertIn("LOG TERBARU", reply["text"])
        self.assertIn("pesan 20", reply["text"])
        self.assertIn("<pre>", reply["text"])
        assert_html_valid(self, reply["text"])

    async def test_count_argument_limits_lines(self):
        write_log_lines(self.log_path, self._sample_lines(30))
        update = FakeUpdate()
        await logviewer.logs_cmd(update, FakeContext(args=["5"]))
        text = update.message.replies[0]["text"]
        self.assertIn("5 baris terakhir", text)
        self.assertIn("pesan 30", text)
        self.assertNotIn("pesan 24", text)

    async def test_count_is_capped(self):
        # baris pendek supaya hasil 200 baris masih muat sebagai pesan
        write_log_lines(self.log_path, [f"baris {i}" for i in range(1, 251)])
        update = FakeUpdate()
        await logviewer.logs_cmd(update, FakeContext(args=["99999"]))
        self.assertIn(f"{logviewer.MAX_LINES} baris terakhir",
                      update.message.replies[0]["text"])

    async def test_level_filter(self):
        write_log_lines(self.log_path, self._sample_lines(30))
        update = FakeUpdate()
        await logviewer.logs_cmd(update, FakeContext(args=["ERROR"]))

        text = update.message.replies[0]["text"]
        self.assertIn("filter level ERROR", text)
        self.assertIn("pesan 30", text)      # 30 habis dibagi 5 → ERROR
        self.assertIn(" - ERROR - ", text)
        self.assertNotIn(" - INFO - ", text)  # baris INFO tersaring keluar

    async def test_level_alias_warn(self):
        write_log_lines(self.log_path, self._sample_lines(30))
        update = FakeUpdate()
        await logviewer.logs_cmd(update, FakeContext(args=["WARN"]))
        text = update.message.replies[0]["text"]
        self.assertIn("filter level WARNING", text)

    async def test_invalid_argument_shows_usage(self):
        write_log_lines(self.log_path, self._sample_lines())
        update = FakeUpdate()
        await logviewer.logs_cmd(update, FakeContext(args=["--shell", "rm"]))
        text = update.message.replies[0]["text"]
        self.assertIn("Pakai:", text)
        self.assertNotIn("<pre>", text)

    async def test_missing_file_gives_informative_message(self):
        update = FakeUpdate()
        await logviewer.logs_cmd(update, FakeContext(args=[]))
        text = update.message.replies[0]["text"]
        self.assertIn("belum ditemukan", text)
        self.assertIn("journalctl", text)

    async def test_empty_file_gives_informative_message(self):
        write_log_lines(self.log_path, [])
        update = FakeUpdate()
        await logviewer.logs_cmd(update, FakeContext(args=[]))
        self.assertIn("Tidak ada baris log", update.message.replies[0]["text"])

    async def test_long_output_is_sent_as_txt_file(self):
        long_lines = [
            f"2026-01-01 00:00:00 - tiktok_bot - INFO - baris panjang nomor {i} " + "x" * 120
            for i in range(60)
        ]
        write_log_lines(self.log_path, long_lines)
        update = FakeUpdate()
        await logviewer.logs_cmd(update, FakeContext(args=[]))

        self.assertEqual(update.message.replies, [], "pesan panjang seharusnya tidak dikirim sebagai teks")
        self.assertEqual(len(update.message.documents), 1)
        doc = update.message.documents[0]
        self.assertTrue(doc["filename"].endswith(".txt"))
        self.assertIn("baris panjang nomor 59", doc["data"].decode("utf-8"))

    async def test_secrets_are_redacted_before_sending(self):
        fake_token = "123456789:AAHdqTcvd1OmGW9EsKge3cVw8ftaP4g1mZt"
        lines = [
            f"2026-01-01 00:00:00 - tiktok_bot - ERROR - koneksi putus token {fake_token}",
            f"2026-01-01 00:00:01 - tiktok_bot - ERROR - BOT_TOKEN={fake_token} gagal",
            "2026-01-01 00:00:02 - tiktok_bot - ERROR - ILOVEPDF_SECRET_KEY=rahasia123",
            "2026-01-01 00:00:03 - tiktok_bot - INFO - Download completed for user 111",
        ]
        write_log_lines(self.log_path, lines)
        update = FakeUpdate()
        await logviewer.logs_cmd(update, FakeContext(args=[]))
        text = update.message.replies[0]["text"]

        self.assertNotIn(fake_token, text)
        self.assertNotIn("rahasia123", text)
        self.assertIn("[REDACTED]", text)
        self.assertIn("Download completed for user 111", text)  # isi log normal utuh


class RedactionTests(unittest.TestCase):
    def test_bot_token_patterns_removed(self):
        token = "123456789:AAHdqTcvd1OmGW9EsKge3cVw8ftaP4g1mZt"
        for line in (f"token {token}", f"BOT_TOKEN={token}", f"BOT_TOKEN: {token}"):
            cleaned = logviewer.redact(line)
            self.assertNotIn(token, cleaned, line)

    def test_key_password_and_jwt_removed(self):
        cases = [
            "ILOVEPDF_SECRET_KEY=abc-123-xyz",
            "api_key: hunter2secret",
            "password = hunter2",
            "INSTAGRAM_COOKIEFILE=sessionid=abc123def",
            'authorization: Bearer abcdef1234567890',
            "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijk",
        ]
        for line in cases:
            cleaned = logviewer.redact(line)
            for secret in ("abc-123-xyz", "hunter2secret", "hunter2",
                           "sessionid=abc123def", "abcdef1234567890",
                           "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijk"):
                if secret in line:
                    self.assertNotIn(secret, cleaned, line)

    def test_normal_log_line_survives(self):
        line = "2026-01-01 00:00:00 - tiktok_bot - INFO - User 111 requested TikTok URL"
        self.assertEqual(logviewer.redact(line), line)


class TailReadingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "bot.log")

    def test_reads_last_n_lines(self):
        write_log_lines(self.path, [f"baris {i}" for i in range(1, 1001)])
        lines = logviewer.tail_lines(self.path, 50)
        self.assertEqual(len(lines), 50)
        self.assertEqual(lines[-1], "baris 1000")
        self.assertEqual(lines[0], "baris 951")

    def test_level_filter_reads_backwards(self):
        write_log_lines(self.path, [
            f"2026-01-01 00:00:00 - tiktok_bot - {'ERROR' if i % 2 else 'INFO'} - pesan {i}"
            for i in range(1, 101)
        ])
        lines = logviewer.tail_lines(self.path, 10, level="ERROR")
        self.assertEqual(len(lines), 10)
        self.assertTrue(all(" - ERROR - " in line for line in lines))
        # baris genap INFO, ganjil ERROR → ERROR terakhir adalah 99
        self.assertIn("pesan 99", lines[-1])

    def test_large_file_is_never_read_whole(self):
        """Ekor file 2MB+ tetap terbaca tanpa memuat seluruh file."""
        write_log_lines(self.path, [
            "2026-01-01 00:00:00 - tiktok_bot - INFO - " + "z" * 200 + f" {i}"
            for i in range(12000)  # ~2.7MB
        ])
        lines = logviewer.tail_lines(self.path, 5)
        self.assertEqual(len(lines), 5)
        self.assertTrue(lines[-1].endswith(" 11999"))

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            logviewer.tail_lines(os.path.join(self.tmp.name, "hilang.log"), 10)


class FileLoggingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log_path = os.path.join(self.tmp.name, "logs", "bot.log")
        patcher = mock.patch.object(logviewer, "LOG_FILE", self.log_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._remove_handlers)

    def _remove_handlers(self):
        import logging

        absolute = os.path.abspath(self.log_path)
        for handler in list(logging.getLogger().handlers):
            if getattr(handler, "baseFilename", None) == absolute:
                logging.getLogger().removeHandler(handler)
                handler.close()

    def test_creates_private_file_and_is_idempotent(self):
        import logging

        self.assertTrue(logviewer.setup_file_logging())
        self.assertTrue(logviewer.setup_file_logging())  # dipanggil dua kali → aman
        self.assertTrue(os.path.exists(self.log_path))

        root = logging.getLogger()
        absolute = os.path.abspath(self.log_path)
        handlers = [h for h in root.handlers
                    if getattr(h, "baseFilename", None) == absolute]
        self.assertEqual(len(handlers), 1, "handler duplikat → log dobel")

        mode = stat.S_IMODE(os.stat(self.log_path).st_mode)
        self.assertEqual(mode & 0o077, 0, "file log harus 0600")

    def test_writes_readable_lines(self):
        import logging

        self.assertTrue(logviewer.setup_file_logging())
        logging.getLogger("tiktok_bot").info("baris uji untuk /logs")
        for handler in logging.getLogger().handlers:
            handler.flush()

        lines = logviewer.tail_lines(self.log_path, 5)
        self.assertTrue(any("baris uji untuk /logs" in line for line in lines), lines)


# ---------------------------------------------------------------------------
# /stats
# ---------------------------------------------------------------------------

class StatsRecordingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "stats.json")
        patcher = mock.patch.object(stats, "STATS_FILE", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        stats._reset()
        self.addCleanup(stats._reset)
        stats.enable()

    def test_counts_and_unique_users(self):
        stats.record("tiktok", 111, True)
        stats.record("tiktok", 111, False)
        stats.record("pdf", 222, True)

        data = stats.snapshot()
        self.assertEqual(data["requests"], {"total": 3, "ok": 2, "failed": 1})
        self.assertEqual(data["unique_users"], [111, 222])
        self.assertEqual(data["features"]["tiktok"],
                         {"total": 2, "ok": 1, "failed": 1})
        self.assertEqual(data["features"]["pdf"],
                         {"total": 1, "ok": 1, "failed": 0})
        self.assertIsNotNone(data["last_request_at"])
        self.assertIsNotNone(data["since"])

    def test_statistics_survive_restart(self):
        stats.record("instagram", 111, True)
        stats.record("instagram", 111, True)

        stats._reset()   # simulasi restart proses bot
        stats.enable()
        data = stats.snapshot()
        self.assertEqual(data["requests"]["total"], 2)
        self.assertEqual(data["features"]["instagram"]["ok"], 2)
        self.assertEqual(data["unique_users"], [111])

    def test_persistence_is_atomic_and_private(self):
        stats.record("word", 111, True)
        self.assertTrue(os.path.exists(self.path))
        self.assertFalse(os.path.exists(self.path + ".tmp"), "file .tmp tertinggal")

        mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.assertEqual(mode & 0o077, 0, "stats.json harus 0600")

        with open(self.path, encoding="utf-8") as fh:
            stored = json.load(fh)
        # hanya metadata hitungan — tidak ada username/teks pesan/dataset pribadi
        self.assertLessEqual(
            set(stored),
            {"version", "since", "last_request_at", "requests",
             "unique_users", "features"},
        )

    def test_corrupt_file_quarantined_not_fatal(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{rusak bukan json")
        stats._reset()
        stats.enable()

        data = stats.snapshot()
        self.assertEqual(data["requests"]["total"], 0)
        self.assertTrue(os.path.exists(self.path + ".corrupt"))
        stats.record("tiktok", 1, True)   # tetap bisa mencatat setelahnya
        self.assertEqual(stats.snapshot()["requests"]["total"], 1)

    def test_disabled_by_default_never_writes(self):
        stats._reset()   # kondisi normal saat module dipakai tanpa main()
        stats.record("tiktok", 111, True)
        self.assertFalse(os.path.exists(self.path),
                         "impor modul / test tidak boleh menulis stats produksi")
        self.assertEqual(stats.snapshot()["requests"]["total"], 0)

    def test_record_never_raises(self):
        with mock.patch.object(stats, "STATS_FILE",
                               os.path.join(self.tmp.name, "mustahil", "x", "stats.json")):
            with mock.patch.object(stats.os, "makedirs", side_effect=OSError("disk penuh")):
                stats.record("tiktok", 111, True)  # tidak boleh melempar
        self.assertGreaterEqual(stats.snapshot()["requests"]["total"], 1)


class StatsTrackWrapperTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(
            stats, "STATS_FILE", os.path.join(self.tmp.name, "stats.json"))
        patcher.start()
        self.addCleanup(patcher.stop)
        stats._reset()
        self.addCleanup(stats._reset)
        stats.enable()

    async def test_success_and_failure_counted(self):
        async def ok_handler(update, context):
            return "hasil"

        async def bad_handler(update, context):
            raise RuntimeError("gagal")

        wrapped_ok = stats.track("uji_ok", ok_handler)
        wrapped_bad = stats.track("uji_bad", bad_handler)

        self.assertEqual(await wrapped_ok(FakeUpdate(1), None), "hasil")
        with self.assertRaises(RuntimeError):
            await wrapped_bad(FakeUpdate(2), None)

        data = stats.snapshot()
        self.assertEqual(data["features"]["uji_ok"]["ok"], 1)
        self.assertEqual(data["features"]["uji_bad"]["failed"], 1)
        self.assertEqual(data["requests"], {"total": 2, "ok": 1, "failed": 1})
        self.assertEqual(data["unique_users"], [1, 2])

    async def test_mark_failed_counts_as_failure_without_raising(self):
        async def source_down(update, context):
            stats.mark_failed()

        wrapped = stats.track("uji_sumber", source_down)
        await wrapped(FakeUpdate(3), None)  # tidak melempar

        data = stats.snapshot()
        self.assertEqual(data["features"]["uji_sumber"], {"total": 1, "ok": 0, "failed": 1})

    async def test_handler_recording_itself_is_not_counted_twice(self):
        async def manual(update, context):
            stats.record("manual", 4, False)

        wrapped = stats.track("manual", manual)
        await wrapped(FakeUpdate(4), None)

        data = stats.snapshot()
        self.assertEqual(data["features"]["manual"], {"total": 1, "ok": 0, "failed": 1})

    async def test_wrapper_keeps_handler_metadata(self):
        async def named_handler(update, context):
            pass

        self.assertEqual(stats.track("x", named_handler).__name__, "named_handler")

    async def test_record_flag_does_not_leak_between_calls(self):
        async def failing(update, context):
            stats.mark_failed()

        async def fine(update, context):
            pass

        await stats.track("a", failing)(FakeUpdate(5), None)
        await stats.track("a", fine)(FakeUpdate(5), None)

        data = stats.snapshot()
        self.assertEqual(data["features"]["a"]["failed"], 1)
        self.assertEqual(data["features"]["a"]["ok"], 1)


class StatsCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(
            stats, "STATS_FILE", os.path.join(self.tmp.name, "stats.json"))
        patcher.start()
        self.addCleanup(patcher.stop)
        stats._reset()
        self.addCleanup(stats._reset)
        stats.enable()

    async def test_message_shows_breakdown(self):
        stats.record("tiktok", 111, True)
        stats.record("tiktok", 111, False)
        stats.record("word", 222, True)
        stats.record("manga", 333, True)

        update = FakeUpdate()
        await stats.stats_cmd(update, FakeContext())

        reply = update.message.replies[0]
        self.assertEqual(reply.get("parse_mode"), "HTML")
        text = reply["text"]
        for token in ("STATISTIK BOT", "Permintaan", "Total diproses",
                      "Berhasil", "Gagal", "Pengguna unik", "TikTok",
                      "Word → PDF", "/manga", "Sejak"):
            self.assertIn(token, text)
        assert_html_valid(self, text)
        self.assertLess(len(text), 4096)

    async def test_empty_state_is_explained(self):
        update = FakeUpdate()
        await stats.stats_cmd(update, FakeContext())
        text = update.message.replies[0]["text"]
        self.assertIn("Belum ada permintaan", text)
        assert_html_valid(self, text)


# ---------------------------------------------------------------------------
# Integrasi: registrasi handler dan daftar fitur
# ---------------------------------------------------------------------------

class MonitorRoutingTests(unittest.TestCase):
    def _handlers(self):
        from telegram.ext import Application

        captured = {}

        def fake_run(self, *args, **kwargs):
            captured["handlers"] = self.handlers

        with mock.patch.object(Application, "run_polling", fake_run):
            bot.main()
        return captured["handlers"][0]

    def test_monitor_commands_registered(self):
        callbacks = [getattr(handler, "callback", None).__name__
                     for handler in self._handlers()]
        for expected in ("status_cmd", "health_cmd", "stats_cmd", "logs_cmd"):
            self.assertIn(expected, callbacks, f"handler belum terdaftar: {expected}")
        # command monitoring harus terdaftar sebelum handler teks biasa
        self.assertLess(callbacks.index("status_cmd"), callbacks.index("handle_message"))
        # fitur lama tidak hilang
        for expected in ("start", "feature", "anime", "restart_bot",
                         "pdf_command", "word_command", "handle_message"):
            self.assertIn(expected, callbacks)

    def test_commands_match_when_called_from_telegram(self):
        from datetime import datetime, timezone

        from telegram import Bot, Chat, Message, MessageEntity, Update, User

        bot_api = Bot(token="TEST_TOKEN_NOT_A_REAL_BOT")
        bot_api._bot_user = User(id=1, is_bot=True, first_name="Test", username="test_bot")

        for command in ("status", "health", "logs", "stats"):
            message = Message(
                message_id=1,
                date=datetime.now(timezone.utc),
                chat=Chat(id=1, type="private"),
                from_user=User(id=1, first_name="U", is_bot=False),
                text=f"/{command}",
                entities=[MessageEntity(type="bot_command", offset=0, length=len(command) + 1)],
            )
            message.set_bot(bot_api)
            update = Update(update_id=1, message=message)
            handler = next(
                h for h in self._handlers()
                if getattr(h, "callback", None).__name__ in (
                    "status_cmd", "health_cmd", "logs_cmd", "stats_cmd")
                and command in getattr(h, "commands", set())
            )
            self.assertTrue(handler.check_update(update),
                            f"/{command} tidak dikenali CommandHandler")

    def test_feature_list_mentions_monitoring(self):
        text = bot._build_feature_text()
        for token in ("Server Monitor", "/status", "/health", "/stats", "/logs"):
            self.assertIn(token, text)
        assert_html_valid(self, text)
        self.assertLess(len(text), 4096)


class StatsIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Statistik bertambah mengikuti hasil operasi nyata (unduhan & konversi)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(
            stats, "STATS_FILE", os.path.join(self.tmp.name, "stats.json"))
        patcher.start()
        self.addCleanup(patcher.stop)
        stats._reset()
        self.addCleanup(stats._reset)
        stats.enable()

    async def test_successful_download_is_counted(self):
        media_path = os.path.join(self.tmp.name, "media.jpg")
        result = SimpleNamespace(media_items=[(media_path, "photo")],
                                 author=None, description=None)

        async def fake_download(*args, **kwargs):
            return result

        async def fake_send(message, items, caption):
            return None

        update = FakeUpdate(user_id=42_424_242)
        update.message.text = "https://vt.tiktok.com/abcdef/"
        with mock.patch.object(bot, "_run_download", new=fake_download), \
             mock.patch.object(bot, "_send_media", new=fake_send), \
             mock.patch.object(bot, "cleanup_job", new=lambda *a, **k: None):
            await bot.handle_message(update, None)

        data = stats.snapshot()
        self.assertEqual(data["features"].get("tiktok"),
                         {"total": 1, "ok": 1, "failed": 0})
        self.assertEqual(data["unique_users"], [42_424_242])

    async def test_failed_download_is_counted_as_failed(self):
        async def fake_download(*args, **kwargs):
            raise bot.DownloadError("Media tidak ditemukan untuk link ini.")

        update = FakeUpdate(user_id=42_424_243)
        update.message.text = "https://vt.tiktok.com/abcdef/"
        with mock.patch.object(bot, "_run_download", new=fake_download), \
             mock.patch.object(bot, "cleanup_job", new=lambda *a, **k: None):
            await bot.handle_message(update, None)

        data = stats.snapshot()
        self.assertEqual(data["features"].get("tiktok"),
                         {"total": 1, "ok": 0, "failed": 1})
        # user tetap mendapat pesan galat yang jelas
        self.assertTrue(any("Gagal" in edit for edit in update.message.edits))

    async def test_successful_conversion_is_counted(self):
        async def fake_download_document(context, document, dest_path, user_id):
            with open(dest_path, "wb") as fh:
                fh.write(b"%PDF-1.4 fake")

        async def fake_convert(src, dst):
            with open(dst, "wb") as fh:
                fh.write(b"docx")

        document = SimpleNamespace(file_name="contoh.pdf",
                                   mime_type="application/pdf",
                                   file_size=16)
        update = FakeUpdate(user_id=42_424_244)
        update.message.document = document

        with mock.patch.object(bot, "_download_document", new=fake_download_document), \
             mock.patch.object(bot, "inspect_pdf", return_value=True), \
             mock.patch.object(bot, "convert_pdf_to_word", new=fake_convert):
            await bot.pdf_command(update, None)

        data = stats.snapshot()
        self.assertEqual(data["features"].get("pdf"),
                         {"total": 1, "ok": 1, "failed": 0})

    async def test_rejected_conversion_is_counted_as_failed(self):
        document = SimpleNamespace(file_name="bukan-dokumen.txt",
                                   mime_type="text/plain",
                                   file_size=10)
        update = FakeUpdate(user_id=42_424_245)
        update.message.document = document

        await bot.pdf_command(update, None)  # validasi menolak tipe file

        data = stats.snapshot()
        self.assertEqual(data["features"].get("pdf"),
                         {"total": 1, "ok": 0, "failed": 1})


class MonitoringAccessTests(unittest.IsolatedAsyncioTestCase):
    """User biasa tetap bisa memakai /status, /health, /stats — hanya /logs yang dkk admin."""

    async def test_regular_user_can_use_status(self):
        update = FakeUpdate(user_id=12_345_678)  # bukan admin
        measurements = sample_measurements()
        with mock.patch.object(monitor, "sample_cpu", return_value=measurements[0]), \
             mock.patch.object(monitor, "read_memory", return_value=measurements[1]), \
             mock.patch.object(monitor, "read_disk", return_value=measurements[2]), \
             mock.patch.object(monitor, "read_uptime", return_value=measurements[3]):
            await bot.status_cmd(update, None)
        self.assertIn("STATUS SERVER", update.message.replies[0]["text"])

    async def test_regular_user_can_use_stats(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(stats, "STATS_FILE",
                               os.path.join(tmp.name, "stats.json")):
            stats._reset()
            self.addCleanup(stats._reset)
            update = FakeUpdate(user_id=12_345_678)
            await bot.stats_cmd(update, None)
        self.assertIn("STATISTIK BOT", update.message.replies[0]["text"])

    async def test_non_admin_still_denied_for_logs_only(self):
        """Gerbang admin hanya ada di /logs; handler lain tak punya cek admin."""
        update = FakeUpdate(user_id=12_345_678)
        with mock.patch.object(logviewer, "is_admin", return_value=False), \
             mock.patch.object(logviewer, "tail_lines",
                               side_effect=AssertionError("file log dibaca non-admin!")):
            await bot.logs_cmd(update, FakeContext(args=[]))
        self.assertIn("tidak punya akses", update.message.replies[0]["text"])


class RestartAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_admin_cannot_restart(self):
        update = FakeUpdate(user_id=1)
        with mock.patch.object(bot, "is_admin", return_value=False), \
             mock.patch.object(bot.subprocess, "Popen",
                               side_effect=AssertionError("restart dijalankan non-admin!")) as popen:
            await bot.restart_bot(update, FakeContext())

        self.assertIn("tidak punya akses", update.message.replies[0]["text"])
        popen.assert_not_called()

    async def test_admin_can_restart_without_touching_real_system(self):
        update = FakeUpdate(user_id=1)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(bot, "is_admin", return_value=True), \
             mock.patch.object(bot, "DOWNLOAD_DIR", tmp.name), \
             mock.patch.object(bot.subprocess, "Popen") as popen:
            await bot.restart_bot(update, FakeContext())

        popen.assert_called_once()
        self.assertTrue(os.path.exists(os.path.join(tmp.name, ".restart_flag")))


# ---------------------------------------------------------------------------
# Helper kecil
# ---------------------------------------------------------------------------

class HelperTests(unittest.TestCase):
    def test_format_duration(self):
        self.assertEqual(format_duration(None), "n/a")
        self.assertEqual(format_duration(0), "00s")
        self.assertEqual(format_duration(59), "59s")
        self.assertEqual(format_duration(60), "1m 00s")
        self.assertEqual(format_duration(90061), "1d 1h 1m 01s")
        self.assertEqual(format_duration(-5), "00s")

    def test_human_bytes(self):
        self.assertEqual(human_bytes(None), "n/a")
        self.assertEqual(human_bytes(0), "0 B")
        self.assertEqual(human_bytes(1024), "1.0 KB")
        self.assertEqual(human_bytes(5 * 1024 ** 3), "5.0 GB")


class WibTimeTests(unittest.TestCase):
    """Semua jam yang ditampilkan user harus WIB (UTC+07:00), bukan UTC server."""

    def test_wib_offset_and_label(self):
        self.assertEqual(WIB.utcoffset(None).total_seconds(), 7 * 3600)
        self.assertEqual(now_wib().tzname(), "WIB")
        self.assertEqual(now_wib().utcoffset().total_seconds(), 7 * 3600)

    def test_wib_text_converts_from_other_timezones(self):
        from datetime import datetime, timezone
        utc_moment = datetime(2026, 10, 15, 4, 14, tzinfo=timezone.utc)
        self.assertEqual(wib_text(utc_moment), "2026-10-15 11:14 WIB")

    def test_status_timestamp_is_wib(self):
        text = monitor._build_status_text(*sample_measurements())
        self.assertIn("Diperiksa:", text)
        self.assertIn("WIB", text)
        self.assertNotIn("UTC", text)

    def test_health_timestamp_is_wib(self):
        text = monitor._build_health_text(
            [monitor.Check("Proses bot", "OK", "berjalan")])
        self.assertIn("Diperiksa:", text)
        self.assertIn("WIB", text)
        self.assertNotIn("UTC", text)

    def test_stats_last_request_is_shown_in_wib(self):
        self.assertEqual(stats._format_ts("2026-10-15T04:14:00+00:00"),
                         "15 Oct 2026 11:14 WIB")

    def test_log_asctime_is_wib(self):
        # asctime tiap baris log (yang dibaca /logs) harus jam dinding WIB
        record = logging.LogRecord("n", logging.INFO, "p", 1, "cek", None, None)
        record.created = 0  # epoch 1970-01-01 00:00 UTC → 07:00 WIB
        line = logging.Formatter(logviewer.LOG_FORMAT).format(record)
        self.assertTrue(line.startswith("1970-01-01 07:00:00"), line)

    def test_log_filename_timestamp_is_wib(self):
        from datetime import datetime, timezone
        stamp = datetime(2026, 10, 15, 4, 14, tzinfo=timezone.utc)
        with mock.patch.object(logviewer, "now_wib", return_value=stamp.astimezone(WIB)):
            self.assertEqual(logviewer.make_log_filename(), "bot-logs-20261015-111400.txt")


if __name__ == "__main__":
    unittest.main(verbosity=2)
