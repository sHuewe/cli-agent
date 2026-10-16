from __future__ import annotations

import io
import subprocess

import pytest
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

from cli_agent import pdf_text
from cli_agent.config import McpServerConfig
from cli_agent.os_operations import Workspace, WorkspaceError


def _pdf(*texts: str, encrypted: bool = False) -> bytes:
    """Synthetic fixtures only; no private documents in the repository."""
    writer = PdfWriter()
    for text in texts:
        page = writer.add_blank_page(width=300, height=300)
        if text:
            font = DictionaryObject({
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            })
            page[NameObject("/Resources")] = DictionaryObject({
                NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
            })
            stream = DecodedStreamObject()
            stream.set_data(f"BT /F1 12 Tf 20 200 Td ({text}) Tj ET".encode("ascii"))
            page[NameObject("/Contents")] = writer._add_object(stream)
    if encrypted:
        writer.encrypt("password")
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _workspace(tmp_path):
    return Workspace.from_directory(tmp_path, McpServerConfig(name="os"))


def test_read_file_pdf_mixed_pages_and_unchanged_source(tmp_path):
    data = _pdf("Hello PDF", "", "Final page")
    path = tmp_path / "document.PDF"
    path.write_bytes(data)
    text = _workspace(tmp_path).read_file(path.name)
    assert isinstance(text, str)
    assert "--- Seite 1 von 3 ---\nHello PDF" in text
    assert "--- Seite 2 von 3 ---\n[Kein extrahierbarer Text" in text
    assert "--- Seite 3 von 3 ---\nFinal page" in text
    assert path.read_bytes() == data


@pytest.mark.parametrize("data, message", [
    (b"not a PDF", "nicht vollständig"),
    (_pdf("secret", encrypted=True), "Verschlüsselte"),
])
def test_read_file_pdf_errors(tmp_path, data, message):
    (tmp_path / "document.pdf").write_bytes(data)
    with pytest.raises(WorkspaceError, match=message):
        _workspace(tmp_path).read_file("document.pdf")


def test_empty_pdf_and_blank_page():
    assert "keine Seiten" in pdf_text._extract_text(_pdf())
    assert "OCR ist nicht verfügbar" in pdf_text._extract_text(_pdf(""))


def test_page_limit(monkeypatch):
    monkeypatch.setattr(pdf_text, "MAX_PDF_PAGES", 1)
    with pytest.raises(pdf_text.PdfTextError, match="Seiten"):
        pdf_text._extract_text(_pdf("first", "second"))


def test_output_limit_counts_markers(monkeypatch):
    data = _pdf("hello")
    length = len(pdf_text._extract_text(data))
    monkeypatch.setattr(pdf_text, "MAX_PDF_TEXT_CHARS", length)
    assert len(pdf_text._extract_text(data)) == length
    monkeypatch.setattr(pdf_text, "MAX_PDF_TEXT_CHARS", length - 1)
    with pytest.raises(pdf_text.PdfTextError, match="Ausgabelimit"):
        pdf_text._extract_text(data)


def test_size_limit_before_starting_parser(tmp_path, monkeypatch):
    monkeypatch.setattr(pdf_text, "MAX_PDF_BYTES", 10)
    (tmp_path / "large.pdf").write_bytes(b"x" * 11)
    with pytest.raises(WorkspaceError, match="Leselimit"):
        _workspace(tmp_path).read_file("large.pdf")


def test_pdf_worker_uses_minimal_environment(tmp_path, monkeypatch):
    path = tmp_path / "document.pdf"
    path.write_bytes(_pdf("hello"))
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-reach-worker")
    monkeypatch.setenv("PYTHONPATH", "must-not-reach-worker")
    captured = {}

    def run(*args, **kwargs):
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(args[0], 0, b"ok", b"")

    monkeypatch.setattr(pdf_text.subprocess, "run", run)

    assert pdf_text.read_pdf_text(path) == "ok"
    assert "AWS_SECRET_ACCESS_KEY" not in captured["env"]
    assert "PYTHONPATH" not in captured["env"]
    assert captured["env"]["PYTHONSAFEPATH"] == "1"


def test_timeout_is_workspace_error(tmp_path, monkeypatch):
    (tmp_path / "document.pdf").write_bytes(_pdf("hello"))

    def timeout(*args, **kwargs):
        assert kwargs["timeout"] == pdf_text.PDF_TIMEOUT_SECONDS
        assert kwargs["input"].startswith(b"%PDF")
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(pdf_text.subprocess, "run", timeout)
    with pytest.raises(WorkspaceError, match="Zeitlimit"):
        _workspace(tmp_path).read_file("document.pdf")


def test_pdf_cannot_be_written_as_text(tmp_path):
    path = tmp_path / "document.pdf"
    data = _pdf("original")
    path.write_bytes(data)
    with pytest.raises(WorkspaceError):
        _workspace(tmp_path).write_file(path.name, "replacement")
    assert path.read_bytes() == data


@pytest.mark.parametrize("name", [".env.pdf", ".git/document.pdf", "service.log.pdf"])
def test_pdf_keeps_sensitive_path_protection(tmp_path, name):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_pdf("secret"))
    with pytest.raises(WorkspaceError, match="Secret-/Credential"):
        _workspace(tmp_path).read_file(name)


def test_pdf_keeps_path_and_hardlink_protection(tmp_path):
    import os

    path = tmp_path / "document.pdf"
    path.write_bytes(_pdf("secret"))
    os.link(path, tmp_path / "linked.pdf")
    with pytest.raises(WorkspaceError, match="Hardlinks"):
        _workspace(tmp_path).read_file("linked.pdf")
    with pytest.raises(WorkspaceError, match="relativ"):
        _workspace(tmp_path).read_file(str(path))
    with pytest.raises(WorkspaceError, match="enthalten"):
        _workspace(tmp_path).read_file("../document.pdf")


@pytest.mark.parametrize("data,message", [
    (b"invalid PDF", "nicht vollständig"),
    (_pdf("secret", encrypted=True), "Verschlüsselte"),
])
def test_parser_errors_are_sanitized(data, message):
    with pytest.raises(pdf_text.PdfTextError, match=message) as error:
        pdf_text._extract_text(data)
    assert "secret" not in str(error.value)


def test_worker_rechecks_input_size(monkeypatch):
    monkeypatch.setattr(pdf_text, "MAX_PDF_BYTES", 5)
    with pytest.raises(pdf_text.PdfTextError, match="Leselimit"):
        pdf_text._extract_text(b"123456")


def test_file_growth_after_stat_is_rejected(tmp_path, monkeypatch):
    from pathlib import Path

    path = tmp_path / "growing.pdf"
    path.write_bytes(b"12345")
    monkeypatch.setattr(pdf_text, "MAX_PDF_BYTES", 5)
    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: io.BytesIO(b"123456"))
    with pytest.raises(pdf_text.PdfTextError, match="Leselimit"):
        pdf_text.read_pdf_text(path)


@pytest.mark.parametrize("failure", ["missing", "startup", "crash"])
def test_pdf_worker_and_file_failures(tmp_path, monkeypatch, failure):
    path = tmp_path / "document.pdf"
    if failure != "missing":
        path.write_bytes(_pdf("hello"))

    def run(*args, **kwargs):
        if failure == "startup":
            raise OSError("cannot launch interpreter")
        return subprocess.CompletedProcess(args[0], 1, b"", b"")

    monkeypatch.setattr(pdf_text.subprocess, "run", run)
    with pytest.raises(pdf_text.PdfTextError, match="unerwartet" if failure == "crash" else "nicht gelesen"):
        pdf_text.read_pdf_text(path)


@pytest.mark.parametrize("data,status,expected", [
    (_pdf("hello"), 0, "--- Seite 1 von 1 ---\nhello"),
    (b"broken", 2, "PDF konnte nicht vollständig als Text gelesen werden."),
])
def test_worker_stdio_protocol(monkeypatch, data, status, expected):
    import logging
    from types import SimpleNamespace

    output = io.BytesIO()
    old_disable = logging.root.manager.disable
    try:
        with monkeypatch.context() as patch:
            patch.setattr(pdf_text.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(data)))
            patch.setattr(pdf_text.sys, "stdout", SimpleNamespace(buffer=output))
            assert pdf_text._main() == status
        assert output.getvalue().decode("utf-8") == expected
    finally:
        logging.disable(old_disable)



def test_pdf_page_range_preserves_original_page_numbers(tmp_path):
    path = tmp_path / "long.pdf"
    path.write_bytes(_pdf("one", "two", "three", "four"))
    workspace = Workspace.from_directory(
        tmp_path, McpServerConfig(name="os", config={"max_pdf_pages": 2})
    )
    with pytest.raises(WorkspaceError, match="Seiten"):
        workspace.read_file("long.pdf")
    result = workspace.read_file("long.pdf", start_page=2, end_page=3)
    assert "--- Seite 2 von 4 ---\ntwo" in result
    assert "--- Seite 3 von 4 ---\nthree" in result
    assert "Seite 1" not in result
    assert "Seite 4" not in result
    assert workspace.read_file("long.pdf", end_page=2).count("--- Seite") == 2
    assert workspace.read_file("long.pdf", start_page=4).count("--- Seite") == 1


@pytest.mark.parametrize("kwargs", [
    {"start_page": 0},
    {"end_page": -1},
    {"start_page": True},
    {"start_page": 3, "end_page": 2},
    {"start_page": 5},
    {"end_page": 5},
])
def test_pdf_page_range_validation(tmp_path, kwargs):
    (tmp_path / "short.pdf").write_bytes(_pdf("one", "two", "three"))
    with pytest.raises(WorkspaceError):
        _workspace(tmp_path).read_file("short.pdf", **kwargs)


def test_pdf_page_limit_caps_selected_range(tmp_path):
    path = tmp_path / "long.pdf"
    path.write_bytes(_pdf("one", "two", "three", "four"))
    workspace = Workspace.from_directory(
        tmp_path, McpServerConfig(name="os", config={"max_pdf_pages": 2})
    )
    with pytest.raises(WorkspaceError, match="Seiten"):
        workspace.read_file("long.pdf", start_page=1, end_page=3)


def test_pdf_options_not_allowed_for_text_files(tmp_path):
    (tmp_path / "test.txt").write_text("hello", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="PDF"):
        _workspace(tmp_path).read_file("test.txt", start_page=1)


def test_worker_page_options_round_trip(monkeypatch):
    from types import SimpleNamespace
    output = io.BytesIO()
    old_disable = __import__("logging").root.manager.disable
    try:
        with monkeypatch.context() as patch:
            patch.setattr(pdf_text.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(_pdf("one", "two", "three"))))
            patch.setattr(pdf_text.sys, "stdout", SimpleNamespace(buffer=output))
            assert pdf_text._main(["--max-pages", "2", "--start-page", "2", "--end-page", "3"]) == 0
        assert "Seite 2 von 3" in output.getvalue().decode("utf-8")
        assert "Seite 1 von 3" not in output.getvalue().decode("utf-8")
    finally:
        __import__("logging").disable(old_disable)


def test_total_page_limit_rejects_small_range_of_large_pdf(monkeypatch):
    monkeypatch.setattr(pdf_text, "MAX_TOTAL_PDF_PAGES", 3)
    data = _pdf("one", "two", "three", "four")
    with pytest.raises(pdf_text.PdfTextError, match="Sicherheitslimit"):
        pdf_text._extract_text(data, start_page=1, end_page=1, max_pages=2)


def test_page_tree_rejects_lying_count_without_expanding_pages():
    from pypdf import PdfReader
    from pypdf.generic import NameObject, NumberObject

    data = _pdf("one", "two", "three")
    reader = PdfReader(io.BytesIO(data))
    pages_root = reader.trailer["/Root"]["/Pages"]
    pages_root[NameObject("/Count")] = NumberObject(2500)
    with pytest.raises(pdf_text.PdfTextError, match="Sicherheitslimit"):
        pdf_text._bounded_page_count(reader)



def test_page_tree_limits_cumulative_pending_queue(monkeypatch):
    """A nested /Pages chain must not accumulate an unbounded sibling queue."""
    from pypdf import PdfReader
    from pypdf.generic import ArrayObject, DictionaryObject, NameObject, NumberObject

    monkeypatch.setattr(pdf_text, "MAX_TOTAL_PDF_PAGES", 3)
    reader = PdfReader(io.BytesIO(_pdf("one")))
    root = reader.trailer["/Root"]["/Pages"]
    current = root
    for _ in range(5):
        child = DictionaryObject({
            NameObject("/Type"): NameObject("/Pages"),
            NameObject("/Count"): NumberObject(1),
            NameObject("/Kids"): ArrayObject(),
        })
        # The last entry is traversed first (LIFO); each earlier sibling
        # remains pending and consumes the cumulative node budget.
        current[NameObject("/Kids")] = ArrayObject([
            DictionaryObject({NameObject("/Type"): NameObject("/Page")}),
            DictionaryObject({NameObject("/Type"): NameObject("/Page")}),
            child,
        ])
        current[NameObject("/Count")] = NumberObject(1)
        current = child
    current[NameObject("/Kids")] = ArrayObject()

    with pytest.raises(pdf_text.PdfTextError, match="Seitenbaum.*Sicherheitslimit"):
        pdf_text._bounded_page_count(reader)
