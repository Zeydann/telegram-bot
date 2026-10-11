"""Konversi dokumen: PDF -> Word (.docx) dan Word -> PDF.

PDF -> Word dijalankan lokal dengan `pdf2docx` karena API resmi iLovePDF
tidak menyediakan tool PDF to Word (lihat daftar tool di
https://developer.ilovepdf.com/docs/api-reference).
Word -> PDF memakai tool `officepdf` dari iLovePDF REST API:
start -> upload -> process -> download -> delete task.
"""

import os
import time
import asyncio
import zipfile
import logging

import httpx
import jwt
import pymupdf
from pdf2docx import Converter

from config import (
    CONVERT_MAX_CONCURRENT,
    CONVERT_MAX_FILE_SIZE,
    CONVERT_TIMEOUT,
    ILOVEPDF_PUBLIC_KEY,
    ILOVEPDF_REGION,
    ILOVEPDF_SECRET_KEY,
)

logger = logging.getLogger(__name__)

API_BASE = "https://api.ilovepdf.com"
ILOVEPDF_TOOL = "officepdf"  # Word/Excel/PowerPoint -> PDF
CLIENT_TAG = "python.telegram-bot"  # dikirim sebagai param `v` (identitas klien)

JWT_ALGORITHM = "HS256"
TOKEN_LIFETIME = 3600  # token berlaku 1 jam sesuai dokumentasi
CLOCK_DELAY = 5400  # buffer jam, sama dengan library resmi iLovePDF

PDF_EXTENSIONS = {".pdf"}
WORD_EXTENSIONS = {".docx", ".doc"}
PDF_MIMES = {"application/pdf"}
WORD_MIMES = {
    "application/msword",
    "application/vnd.ms-word",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
GENERIC_MIMES = {"", "application/octet-stream", "binary/octet-stream"}

# Atribusi dipakai di caption hasil konversi & pesan bantuan
ILOVEPDF_CREDIT = "Word → PDF conversion powered by iLovePDF."

_MISS_PDF = "File ini bukan PDF. Balas dokumen PDF dengan /pdf ya."
_MISS_WORD = "File ini bukan dokumen Word (.docx/.doc). Balas dokumen Word dengan /word ya."
_BAD_PDF = "File PDF rusak atau tidak bisa dibaca."
_BAD_DOCX = "File Word rusak atau bukan .docx yang valid."
_EMPTY = "File-nya kosong, tidak bisa dikonversi."
_NOT_CONFIGURED = (
    "Konversi Word → PDF belum aktif. Set ILOVEPDF_PUBLIC_KEY dan "
    "ILOVEPDF_SECRET_KEY di .env (cara daftarnya ada di README)."
)

# Batasi jumlah konversi berjalan bersamaan.
# Dipekan per event loop karena asyncio mengikat lock ke loop pertama yang memakainya.
_semaphores: dict[asyncio.AbstractEventLoop, asyncio.Semaphore] = {}


def _conversion_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    for old_loop in [item for item in _semaphores if item.is_closed()]:
        _semaphores.pop(old_loop, None)
    sem = _semaphores.get(loop)
    if sem is None:
        sem = asyncio.Semaphore(max(1, CONVERT_MAX_CONCURRENT))
        _semaphores[loop] = sem
    return sem


class ConversionError(Exception):
    """Konversi gagal; pesannya aman ditampilkan langsung ke user."""


def _human_size(limit: int) -> str:
    return f"{limit / (1024 * 1024):g} MB"


def validate_document(file_name, mime_type, file_size, command: str) -> str:
    """Validasi ekstensi, MIME type, dan ukuran file.

    Mengembalikan ekstensi file (mis. ``.pdf``) atau melempar
    :class:`ConversionError` dengan pesan yang bisa dibalas ke user.
    """
    ext = os.path.splitext(file_name or "")[1].lower()
    mime = (mime_type or "").strip().lower()

    if command == "pdf":
        wanted_ext, wanted_mimes, missed = PDF_EXTENSIONS, PDF_MIMES, _MISS_PDF
    else:
        wanted_ext, wanted_mimes, missed = WORD_EXTENSIONS, WORD_MIMES, _MISS_WORD

    if not file_name or ext not in wanted_ext:
        raise ConversionError(missed)
    if mime not in wanted_mimes and mime not in GENERIC_MIMES:
        raise ConversionError(missed)
    if file_size is not None and file_size <= 0:
        raise ConversionError(_EMPTY)
    if file_size and file_size > CONVERT_MAX_FILE_SIZE:
        raise ConversionError(f"Ukuran file melebihi batas {_human_size(CONVERT_MAX_FILE_SIZE)}.")
    return ext


def inspect_pdf(path: str) -> bool:
    """Cek PDF (rusak/terproteksi/kosong).

    Return ``True`` bila ada teks terstruktur, ``False`` bila PDF murni
    hasil scan/gambar (butuh OCR untuk hasil Word yang rapi).
    """
    try:
        doc = pymupdf.open(path)
    except Exception:
        logger.warning("inspect_pdf: file tidak bisa dibuka")
        raise ConversionError(_BAD_PDF)
    try:
        if doc.needs_pass:
            raise ConversionError("PDF-nya diproteksi password. Hapus proteksinya dulu lalu coba lagi.")
        if doc.page_count == 0:
            raise ConversionError(_EMPTY)
        for index in range(min(5, doc.page_count)):
            if doc.load_page(index).get_text().strip():
                return True
        return False
    finally:
        doc.close()


def inspect_word(path: str, ext: str) -> None:
    """Cek file Word .docx (zip rusak / bukan docx). File .doc lama dibiarkan dinilai API."""
    if ext != ".docx":
        return
    try:
        with zipfile.ZipFile(path) as archive:
            if "word/document.xml" not in archive.namelist():
                raise ConversionError(_BAD_DOCX)
    except ConversionError:
        raise
    except Exception:
        logger.warning("inspect_word: bukan docx yang valid")
        raise ConversionError(_BAD_DOCX)


def _pdf_to_docx_sync(src_path: str, dst_path: str) -> None:
    """Konversi blokir PDF -> DOCX; jalankan di thread (asyncio.to_thread)."""
    try:
        cv = Converter(src_path)
    except Exception:
        logger.warning("pdf2docx: gagal membuka PDF")
        raise ConversionError(_BAD_PDF)
    try:
        cv.convert(dst_path, start=0, end=None)
    except Exception:
        logger.warning("pdf2docx: konversi gagal")
        raise ConversionError("PDF gagal dikonversi. File mungkin rusak atau formatnya tidak didukung.")
    finally:
        try:
            cv.close()
        except Exception:
            pass  # jangan menutupi error konversi yang sebenarnya

    if not os.path.exists(dst_path) or os.path.getsize(dst_path) == 0:
        raise ConversionError("Hasil konversi kosong. Coba dengan PDF lain.")


async def convert_pdf_to_word(src_path: str, dst_path: str) -> None:
    """PDF -> DOCX (lokal, pdf2docx)."""
    async with _conversion_semaphore():
        await asyncio.to_thread(_pdf_to_docx_sync, src_path, dst_path)


async def convert_word_to_pdf(src_path: str, dst_path: str, mime_type=None) -> None:
    """DOCX/DOC -> PDF memakai API resmi iLovePDF (tool ``officepdf``)."""
    if not ILOVEPDF_PUBLIC_KEY or not ILOVEPDF_SECRET_KEY:
        raise ConversionError(_NOT_CONFIGURED)
    async with _conversion_semaphore():
        await ILovePDFClient().convert_office_to_pdf(src_path, dst_path, mime_type)


class ILovePDFClient:
    """Klien async ringkas untuk alur task iLovePDF (tanpa library resmi agar tetap async)."""

    def __init__(self, timeout: float | None = None):
        self.timeout = float(timeout or CONVERT_TIMEOUT)

    def _token(self) -> str:
        """JWT self-signed (HS256) sesuai dokumentasi autentikasi iLovePDF."""
        now = int(time.time())
        payload = {
            "iss": "",
            "aud": "",
            "iat": now - CLOCK_DELAY,
            "nbf": now - CLOCK_DELAY,
            "exp": now + TOKEN_LIFETIME + CLOCK_DELAY,
            "jti": ILOVEPDF_PUBLIC_KEY,
        }
        return jwt.encode(payload, ILOVEPDF_SECRET_KEY, algorithm=JWT_ALGORITHM)

    async def _request(self, client: httpx.AsyncClient, method: str, url: str, **kwargs):
        headers = kwargs.pop("headers", None) or {}
        headers.setdefault("Accept", "application/json")
        headers["Authorization"] = f"Bearer {self._token()}"
        try:
            resp = await client.request(method, url, headers=headers, **kwargs)
        except httpx.TimeoutException:
            logger.warning("iLovePDF timeout: %s %s", method, url)
            raise ConversionError("Konversi melebihi batas waktu. Coba lagi nanti.")
        except httpx.HTTPError as exc:
            logger.warning("iLovePDF request error: %s", type(exc).__name__)
            raise ConversionError("Tidak bisa terhubung ke iLovePDF. Coba lagi nanti.")
        if resp.status_code >= 400:
            self._raise_api_error(resp, method, url)
        return resp

    @staticmethod
    def _json(resp: httpx.Response, what: str) -> dict:
        """Baca respons JSON; gagal dibaca berarti respons tidak valid."""
        try:
            data = resp.json()
        except Exception:
            logger.warning("iLovePDF %s: respons bukan JSON", what)
            raise ConversionError("Respons iLovePDF tidak valid. Coba lagi nanti.")
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _raise_api_error(resp: httpx.Response, method: str, url: str) -> None:
        """Terjemahkan status error API ke pesan user (tanpa membocorkan secret)."""
        detail = None
        try:
            body = resp.json()
            if isinstance(body, dict):
                detail = body.get("message") or body.get("type")
        except Exception:
            pass
        # url hanya berisi host + path tool, tidak ada secret/konten dokumen
        logger.warning("iLovePDF error: %s %s -> HTTP %s (%s)", method, url, resp.status_code, detail)

        if resp.status_code == 401:
            raise ConversionError(
                "API key iLovePDF tidak valid. Cek ILOVEPDF_PUBLIC_KEY / ILOVEPDF_SECRET_KEY di .env."
            )
        if resp.status_code == 429:
            raise ConversionError("Batas permintaan iLovePDF tercapai. Coba lagi nanti.")
        if resp.status_code == 400:
            raise ConversionError(
                f"iLovePDF menolak file ini: {detail}"
                if detail
                else "File ditolak iLovePDF (mungkin rusak atau terproteksi password)."
            )
        if resp.status_code >= 500:
            raise ConversionError("Layanan iLovePDF sedang bermasalah. Coba lagi nanti.")
        raise ConversionError("Konversi gagal di sisi iLovePDF. Coba lagi nanti.")

    async def convert_office_to_pdf(self, src_path: str, dst_path: str, mime_type=None) -> None:
        """Jalankan task ``officepdf`` penuh, termasuk hapus task di server."""
        filename = os.path.basename(src_path)
        mime = mime_type if (mime_type or "").lower() in WORD_MIMES else "application/octet-stream"

        region = (ILOVEPDF_REGION or "").strip().lower()
        start_url = f"{API_BASE}/v1/start/{ILOVEPDF_TOOL}" + (f"/{region}" if region else "")

        async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout), follow_redirects=True) as client:
            task = server = None
            try:
                data = self._json(await self._request(client, "GET", start_url), "start")
                server, task = data.get("server"), data.get("task")
                if not server or not task:
                    logger.warning("iLovePDF start: server/task tidak diberikan")
                    raise ConversionError("iLovePDF tidak memberikan server. Coba lagi nanti.")

                with open(src_path, "rb") as fh:
                    payload = fh.read()
                resp = await self._request(
                    client,
                    "POST",
                    f"https://{server}/v1/upload",
                    data={"task": task, "v": CLIENT_TAG},
                    files={"file": (filename, payload, mime)},
                )
                server_filename = self._json(resp, "upload").get("server_filename")
                if not server_filename:
                    logger.warning("iLovePDF upload: server_filename tidak ada")
                    raise ConversionError("Upload ke iLovePDF gagal. Coba lagi nanti.")

                resp = await self._request(
                    client,
                    "POST",
                    f"https://{server}/v1/process",
                    params={"v": CLIENT_TAG},
                    json={
                        "task": task,
                        "tool": ILOVEPDF_TOOL,
                        "files": [{"server_filename": server_filename, "filename": filename}],
                    },
                )
                status = self._json(resp, "process").get("status")
                if status not in ("TaskSuccess", "TaskSuccessWithWarnings"):
                    logger.warning("iLovePDF process status: %s", status)
                    raise ConversionError(
                        "Konversi Word → PDF gagal di iLovePDF. File mungkin rusak atau terproteksi."
                    )

                resp = await self._request(
                    client, "GET", f"https://{server}/v1/download/{task}", params={"v": CLIENT_TAG}
                )
                if "json" in resp.headers.get("content-type", "").lower():
                    raise ConversionError("iLovePDF tidak mengembalikan file PDF. Coba lagi nanti.")
                with open(dst_path, "wb") as out:
                    out.write(resp.content)
            finally:
                # Bersihkan task + file di server iLovePDF (otomatis juga kadaluarsa 1-2 jam)
                if server and task:
                    try:
                        await self._request(client, "DELETE", f"https://{server}/v1/task/{task}")
                    except Exception:
                        pass  # pembersihan server bersifat opsional

        if not os.path.exists(dst_path) or os.path.getsize(dst_path) == 0:
            raise ConversionError("Hasil konversi kosong. Coba lagi nanti.")
