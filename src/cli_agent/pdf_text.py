"""Bounded, read-only PDF text extraction without OCR.

Parsing runs in a disposable process so its timeout can actually stop pypdf.
Only validated file bytes cross into that process, never workspace paths.
"""

from __future__ import annotations

import io
import logging
import os
import subprocess
import sys
from pathlib import Path

MAX_PDF_BYTES = 10_000_000
MAX_PDF_PAGES = 100
MAX_PDF_TEXT_CHARS = 1_000_000
PDF_TIMEOUT_SECONDS = 20

_PDF_WORKER_ENV_NAMES = (
    "PATH",
    "HOME",
    "USERPROFILE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "LANG",
    "LC_ALL",
    "PYTHONIOENCODING",
)


def _pdf_worker_environment() -> dict[str, str]:
    environment = {
        name: os.environ[name]
        for name in _PDF_WORKER_ENV_NAMES
        if os.environ.get(name) is not None
    }
    environment["PYTHONSAFEPATH"] = "1"
    return environment


class PdfTextError(RuntimeError):
    """A PDF could not be safely read as text."""


def _extract_text(data: bytes) -> str:
    from pypdf import PdfReader

    if len(data) > MAX_PDF_BYTES:
        raise PdfTextError(f"PDF überschreitet das Leselimit von {MAX_PDF_BYTES} Bytes.")
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise PdfTextError("Verschlüsselte PDFs werden nicht unterstützt.")
        page_count = len(reader.pages)
        if page_count > MAX_PDF_PAGES:
            raise PdfTextError(f"PDF überschreitet das Limit von {MAX_PDF_PAGES} Seiten.")
        if page_count == 0:
            return "(PDF enthält keine Seiten.)"
        sections = []
        total_chars = 0
        for number, page in enumerate(reader.pages, start=1):
            text = (page.extract_text() or "").strip()
            if not text:
                text = "[Kein extrahierbarer Text auf dieser Seite; OCR ist nicht verfügbar.]"
            section = f"--- Seite {number} von {page_count} ---\n{text}"
            total_chars += len(section) + (2 if sections else 0)
            if total_chars > MAX_PDF_TEXT_CHARS:
                raise PdfTextError(
                    f"PDF-Text überschreitet das Ausgabelimit von {MAX_PDF_TEXT_CHARS} Zeichen."
                )
            sections.append(section)
        return "\n\n".join(sections)
    except PdfTextError:
        raise
    except Exception as exc:
        # Parser exceptions can contain document content. Do not expose them.
        raise PdfTextError("PDF konnte nicht vollständig als Text gelesen werden.") from exc


def read_pdf_text(path: Path) -> str:
    try:
        if path.stat().st_size > MAX_PDF_BYTES:
            raise PdfTextError(f"PDF überschreitet das Leselimit von {MAX_PDF_BYTES} Bytes.")
        with path.open("rb") as stream:
            data = stream.read(MAX_PDF_BYTES + 1)
        if len(data) > MAX_PDF_BYTES:
            raise PdfTextError(f"PDF überschreitet das Leselimit von {MAX_PDF_BYTES} Bytes.")
        result = subprocess.run(
            [sys.executable, "-m", "cli_agent.pdf_text"],
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=PDF_TIMEOUT_SECONDS,
            check=False,
            env=_pdf_worker_environment(),
        )
    except subprocess.TimeoutExpired as exc:
        raise PdfTextError(
            f"PDF-Verarbeitung überschreitet das Zeitlimit von {PDF_TIMEOUT_SECONDS} Sekunden."
        ) from exc
    except OSError as exc:
        raise PdfTextError("PDF konnte nicht gelesen oder die Extraktion nicht gestartet werden.") from exc
    if result.returncode == 2:
        raise PdfTextError(result.stdout.decode("utf-8"))
    if result.returncode != 0:
        raise PdfTextError("PDF-Verarbeitung wurde unerwartet beendet.")
    return result.stdout.decode("utf-8")


def _main() -> int:
    logging.disable(logging.CRITICAL)
    try:
        text = _extract_text(sys.stdin.buffer.read(MAX_PDF_BYTES + 1))
    except PdfTextError as exc:
        sys.stdout.buffer.write(str(exc).encode("utf-8"))
        return 2
    sys.stdout.buffer.write(text.encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
