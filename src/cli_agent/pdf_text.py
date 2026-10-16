"""Bounded, read-only PDF text extraction without OCR.

Parsing runs in a disposable process so its timeout can actually stop pypdf.
Only validated file bytes cross into that process, never workspace paths.
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import subprocess
import sys
from pathlib import Path

MAX_PDF_BYTES = 10_000_000
MAX_PDF_PAGES = 100
MAX_CONFIGURABLE_PDF_PAGES = 2_000
MAX_TOTAL_PDF_PAGES = 2_000
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


def _bounded_page_count(reader: object) -> int:
    """Check the PDF page tree before pypdf expands its flattened pages list."""
    from pypdf.generic import ArrayObject, DictionaryObject

    root = reader.trailer["/Root"]
    page_tree = root["/Pages"]
    pending = [page_tree]
    visited: set[tuple[int, int] | int] = set()
    pages = 0
    nodes = 0
    while pending:
        entry = pending.pop()
        key = (
            (entry.idnum, entry.generation)
            if hasattr(entry, "idnum") and hasattr(entry, "generation")
            else id(entry)
        )
        if key in visited:
            raise PdfTextError("PDF enthält einen zyklischen Seitenbaum.")
        visited.add(key)
        nodes += 1
        if nodes > 2 * MAX_TOTAL_PDF_PAGES + 1:
            raise PdfTextError("PDF-Seitenbaum überschreitet das Sicherheitslimit.")
        node = entry.get_object() if hasattr(entry, "get_object") else entry
        if not isinstance(node, DictionaryObject):
            raise PdfTextError("PDF enthält einen ungültigen Seitenbaum.")
        if str(node.get("/Type")) == "/Page":
            pages += 1
            if pages > MAX_TOTAL_PDF_PAGES:
                raise PdfTextError(
                    f"PDF überschreitet das Sicherheitslimit von {MAX_TOTAL_PDF_PAGES} Gesamtseiten."
                )
            continue
        if str(node.get("/Type")) != "/Pages":
            raise PdfTextError("PDF enthält einen ungültigen Seitenbaum.")
        count = node.get("/Count")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise PdfTextError("PDF enthält eine ungültige Seitenanzahl.")
        if count > MAX_TOTAL_PDF_PAGES:
            raise PdfTextError(
                f"PDF überschreitet das Sicherheitslimit von {MAX_TOTAL_PDF_PAGES} Gesamtseiten."
            )
        kids = node.get("/Kids")
        kids = kids.get_object() if hasattr(kids, "get_object") else kids
        if not isinstance(kids, ArrayObject):
            raise PdfTextError("PDF enthält einen ungültigen Seitenbaum.")
        if len(kids) > MAX_TOTAL_PDF_PAGES or len(kids) + nodes > 2 * MAX_TOTAL_PDF_PAGES + 1:
            raise PdfTextError("PDF-Seitenbaum überschreitet das Sicherheitslimit.")
        pending.extend(kids)
    return pages


def _validate_page_options(
    start_page: int | None,
    end_page: int | None,
    max_pages: int,
) -> None:
    if (
        isinstance(max_pages, bool)
        or not isinstance(max_pages, int)
        or not 1 <= max_pages <= MAX_CONFIGURABLE_PDF_PAGES
    ):
        raise PdfTextError(
            f"max_pdf_pages muss zwischen 1 und {MAX_CONFIGURABLE_PDF_PAGES} liegen."
        )
    for name, number in (("start_page", start_page), ("end_page", end_page)):
        if number is not None and (
            isinstance(number, bool) or not isinstance(number, int) or number < 1
        ):
            raise PdfTextError(f"{name} muss eine positive ganze Zahl sein.")
    if start_page is not None and end_page is not None and start_page > end_page:
        raise PdfTextError("start_page darf nicht größer als end_page sein.")


def _extract_text(
    data: bytes,
    *,
    start_page: int | None = None,
    end_page: int | None = None,
    max_pages: int | None = None,
) -> str:
    from pypdf import PdfReader

    page_limit = MAX_PDF_PAGES if max_pages is None else max_pages
    _validate_page_options(start_page, end_page, page_limit)
    if len(data) > MAX_PDF_BYTES:
        raise PdfTextError(f"PDF überschreitet das Leselimit von {MAX_PDF_BYTES} Bytes.")
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise PdfTextError("Verschlüsselte PDFs werden nicht unterstützt.")
        page_count = _bounded_page_count(reader)
        # Reject conflicting declared counts before asking pypdf to flatten.
        if len(reader.pages) != page_count:
            raise PdfTextError("PDF-Seitenanzahl ist widersprüchlich.")
        if page_count == 0:
            if start_page is not None or end_page is not None:
                raise PdfTextError("PDF enthält keine Seiten für den angeforderten Bereich.")
            return "(PDF enthält keine Seiten.)"
        first = 1 if start_page is None else start_page
        last = page_count if end_page is None else end_page
        if first > page_count or last > page_count:
            raise PdfTextError(
                f"Angeforderter Seitenbereich liegt außerhalb des PDFs "
                f"mit {page_count} Seiten."
            )
        selected_pages = last - first + 1
        if selected_pages > page_limit:
            raise PdfTextError(
                f"PDF-Seitenbereich überschreitet das Limit von {page_limit} Seiten."
            )
        sections = []
        total_chars = 0
        for number in range(first, last + 1):
            page = reader.pages[number - 1]
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


def read_pdf_text(
    path: Path,
    *,
    start_page: int | None = None,
    end_page: int | None = None,
    max_pages: int | None = None,
) -> str:
    page_limit = MAX_PDF_PAGES if max_pages is None else max_pages
    _validate_page_options(start_page, end_page, page_limit)
    try:
        if path.stat().st_size > MAX_PDF_BYTES:
            raise PdfTextError(f"PDF überschreitet das Leselimit von {MAX_PDF_BYTES} Bytes.")
        with path.open("rb") as stream:
            data = stream.read(MAX_PDF_BYTES + 1)
        if len(data) > MAX_PDF_BYTES:
            raise PdfTextError(f"PDF überschreitet das Leselimit von {MAX_PDF_BYTES} Bytes.")
        result = subprocess.run(
            [
                sys.executable, "-m", "cli_agent.pdf_text",
                "--max-pages", str(page_limit),
                *([] if start_page is None else ["--start-page", str(start_page)]),
                *([] if end_page is None else ["--end-page", str(end_page)]),
            ],
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


def _main(argv: list[str] | None = None) -> int:
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser(description="Bounded PDF text extraction worker")
    parser.add_argument("--start-page", type=int, default=None)
    parser.add_argument("--end-page", type=int, default=None)
    parser.add_argument("--max-pages", type=int, default=MAX_PDF_PAGES)
    # argv=None denotes direct test/library usage; the __main__ path passes
    # sys.argv[1:] explicitly so pytest's arguments are not parsed by tests.
    args = parser.parse_args([] if argv is None else argv)
    try:
        _validate_page_options(args.start_page, args.end_page, args.max_pages)
        text = _extract_text(
            sys.stdin.buffer.read(MAX_PDF_BYTES + 1),
            start_page=args.start_page,
            end_page=args.end_page,
            max_pages=args.max_pages,
        )
    except PdfTextError as exc:
        sys.stdout.buffer.write(str(exc).encode("utf-8"))
        return 2
    sys.stdout.buffer.write(text.encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
