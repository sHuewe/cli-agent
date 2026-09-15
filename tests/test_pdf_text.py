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
