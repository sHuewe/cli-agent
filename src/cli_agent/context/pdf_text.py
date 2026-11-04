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
    max_nodes = 2 * MAX_TOTAL_PDF_PAGES + 1
    while pending:
        entry = pending.pop()
        key = (
            (entry.idnum, entry.generation)
            if hasattr(entry, "idnum") and hasattr(entry, "generation")
            else id(entry)
        )
        if key in visited:
            raise PdfTextError("PDF contains a cyclic page tree.")
        visited.add(key)
        nodes += 1
        if nodes > max_nodes:
            raise PdfTextError("PDF page tree exceeds the safety limit.")
        node = entry.get_object() if hasattr(entry, "get_object") else entry
        if not isinstance(node, DictionaryObject):
            raise PdfTextError("PDF contains an invalid page tree.")
        if str(node.get("/Type")) == "/Page":
            pages += 1
            if pages > MAX_TOTAL_PDF_PAGES:
                raise PdfTextError(
                    f"PDF exceeds the safety limit of {MAX_TOTAL_PDF_PAGES} total pages."
                )
            continue
        if str(node.get("/Type")) != "/Pages":
            raise PdfTextError("PDF contains an invalid page tree.")
        count = node.get("/Count")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise PdfTextError("PDF contains an invalid page count.")
        if count > MAX_TOTAL_PDF_PAGES:
            raise PdfTextError(
                f"PDF exceeds the safety limit of {MAX_TOTAL_PDF_PAGES} total pages."
            )
        kids = node.get("/Kids")
        kids = kids.get_object() if hasattr(kids, "get_object") else kids
        if not isinstance(kids, ArrayObject):
            raise PdfTextError("PDF contains an invalid page tree.")
        # Count *all* scheduled entries: processed nodes, entries already
        # waiting in the LIFO stack, and the children about to be enqueued.
        # Checking only nodes + len(kids) misses the cumulative pending queue
        # of deeply nested /Pages trees with dishonest /Count declarations.
        if (
            len(kids) > MAX_TOTAL_PDF_PAGES
            or nodes + len(pending) + len(kids) > max_nodes
        ):
            raise PdfTextError("PDF page tree exceeds the safety limit.")
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
            raise PdfTextError(f"{name} must be a positive integer.")
    if start_page is not None and end_page is not None and start_page > end_page:
        raise PdfTextError("start_page must not exceed end_page.")


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
        raise PdfTextError(f"PDF exceeds the read limit of {MAX_PDF_BYTES} Bytes.")
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise PdfTextError("Encrypted PDFs are not supported.")
        page_count = _bounded_page_count(reader)
        # Reject conflicting declared counts before asking pypdf to flatten.
        if len(reader.pages) != page_count:
            raise PdfTextError("PDF page count is inconsistent.")
        if page_count == 0:
            if start_page is not None or end_page is not None:
                raise PdfTextError("PDF has no pages in the requested range.")
            return "(PDF contains no pages.)"
        first = 1 if start_page is None else start_page
        last = page_count if end_page is None else end_page
        if first > page_count or last > page_count:
            raise PdfTextError(
                f"Requested page range is outside the PDF "
                f"mit {page_count} pages."
            )
        selected_pages = last - first + 1
        if selected_pages > page_limit:
            raise PdfTextError(
                f"PDF page range exceeds the limit of {page_limit} pages."
            )
        sections = []
        total_chars = 0
        for number in range(first, last + 1):
            page = reader.pages[number - 1]
            text = (page.extract_text() or "").strip()
            if not text:
                text = "[No extractable text on this page; OCR is not available.]"
            section = f"--- Seite {number} von {page_count} ---\n{text}"
            total_chars += len(section) + (2 if sections else 0)
            if total_chars > MAX_PDF_TEXT_CHARS:
                raise PdfTextError(
                    f"PDF text exceeds the output limit of {MAX_PDF_TEXT_CHARS} characters."
                )
            sections.append(section)
        return "\n\n".join(sections)
    except PdfTextError:
        raise
    except Exception as exc:
        # Parser exceptions can contain document content. Do not expose them.
        raise PdfTextError("PDF could not be fully extracted as text.") from exc


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
            raise PdfTextError(f"PDF exceeds the read limit of {MAX_PDF_BYTES} Bytes.")
        with path.open("rb") as stream:
            data = stream.read(MAX_PDF_BYTES + 1)
        if len(data) > MAX_PDF_BYTES:
            raise PdfTextError(f"PDF exceeds the read limit of {MAX_PDF_BYTES} Bytes.")
        result = subprocess.run(
            [
                sys.executable, "-m", "cli_agent.context.pdf_text",
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
            f"PDF processing exceeds the time limit of {PDF_TIMEOUT_SECONDS} seconds."
        ) from exc
    except OSError as exc:
        raise PdfTextError("PDF could not be read or extraction could not be started.") from exc
    if result.returncode == 2:
        raise PdfTextError(result.stdout.decode("utf-8"))
    if result.returncode != 0:
        raise PdfTextError("PDF processing terminated unexpectedly.")
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
