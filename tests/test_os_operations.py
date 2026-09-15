from __future__ import annotations

import pytest
from pypdf import PdfWriter

import cli_agent.os_operations as os_operations
from cli_agent.config import McpServerConfig
from cli_agent.os_operations import Workspace, WorkspaceError


def _workspace(tmp_path):
    return Workspace.from_directory(
        tmp_path,
        McpServerConfig(
            name="os",
            config={"allow_write_files": True},
        ),
    )


def _write_text_pdf(path, text: str = "Hello from PDF") -> None:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 12 Tf 72 72 Td ({escaped}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 144] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length "
        + str(len(stream)).encode("ascii")
        + b" >>\nstream\n"
        + stream
        + b"\nendstream",
    ]

    data = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(data))
        data.extend(f"{index} 0 obj\n".encode("ascii"))
        data.extend(obj)
        data.extend(b"\nendobj\n")

    xref_offset = len(data)
    data.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    data.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        data.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    data.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    path.write_bytes(data)


def test_write_file_preserves_lf_line_endings(tmp_path) -> None:
    file_path = tmp_path / "Example.java"
    file_path.write_bytes(b"line1\nline2\n")
    workspace = _workspace(tmp_path)
    workspace.write_file("Example.java", "line1\nchanged\n")
    assert file_path.read_bytes() == b"line1\nchanged\n"


def test_write_file_preserves_crlf_line_endings(tmp_path) -> None:
    file_path = tmp_path / "Example.java"
    file_path.write_bytes(b"line1\r\nline2\r\n")
    workspace = _workspace(tmp_path)
    workspace.write_file("Example.java", "line1\nchanged\n")
    assert file_path.read_bytes() == b"line1\r\nchanged\r\n"


def test_write_file_prefers_dominant_lf_in_mixed_file(tmp_path) -> None:
    file_path = tmp_path / "Example.java"
    file_path.write_bytes(b"line1\nline2\nline3\r\n")
    workspace = _workspace(tmp_path)
    workspace.write_file("Example.java", "line1\nchanged\nline3\n")
    assert file_path.read_bytes() == b"line1\nchanged\nline3\n"


def test_write_file_prefers_dominant_crlf_in_mixed_file(tmp_path) -> None:
    file_path = tmp_path / "Example.java"
    file_path.write_bytes(b"line1\r\nline2\r\nline3\n")
    workspace = _workspace(tmp_path)
    workspace.write_file("Example.java", "line1\nchanged\nline3\n")
    assert file_path.read_bytes() == b"line1\r\nchanged\r\nline3\r\n"


def test_write_file_prefers_lf_on_mixed_line_ending_tie(tmp_path) -> None:
    file_path = tmp_path / "Example.java"
    file_path.write_bytes(b"line1\nline2\r\n")
    workspace = _workspace(tmp_path)
    workspace.write_file("Example.java", "line1\nchanged\n")
    assert file_path.read_bytes() == b"line1\nchanged\n"


def test_write_file_uses_lf_for_new_files(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    workspace.write_file("Example.java", "line1\r\nline2\r\n")
    assert (tmp_path / "Example.java").read_bytes() == b"line1\nline2\n"


@pytest.mark.parametrize(
    "filename",
    [
        "script.py",
        "component.jsx",
        "module.mjs",
        "main.Go",
        "service.PHP",
        "lib.rs",
        "script.lua",
        "build.gradle",
        "App.swift",
        "infra.tf",
        "project.csproj",
        "script.fish",
        "Cargo.lock",
        ".dockerignore",
        "gradlew",
    ],
)
def test_common_source_and_project_files_are_supported_case_insensitively(
    tmp_path, filename
) -> None:
    workspace = _workspace(tmp_path)
    workspace.write_file(filename, "sample\n")
    assert workspace.read_file(filename) == "sample\n"


def test_read_file_extracts_text_from_pdf(tmp_path) -> None:
    _write_text_pdf(tmp_path / "specification.pdf", "Hello from PDF")

    result = _workspace(tmp_path).read_file("specification.pdf")

    assert "--- PDF-Seite 1 ---" in result
    assert "Hello from PDF" in result


def test_read_file_accepts_uppercase_pdf_suffix(tmp_path) -> None:
    _write_text_pdf(tmp_path / "SPECIFICATION.PDF", "Uppercase PDF")

    assert "Uppercase PDF" in _workspace(tmp_path).read_file("SPECIFICATION.PDF")


def test_read_file_rejects_pdf_without_pdf_signature(tmp_path) -> None:
    (tmp_path / "fake.pdf").write_bytes(b"not a pdf")

    with pytest.raises(WorkspaceError, match="PDF-Signatur"):
        _workspace(tmp_path).read_file("fake.pdf")


def test_read_file_reports_pdf_without_extractable_text_instead_of_ocr(tmp_path) -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    with (tmp_path / "scan.pdf").open("wb") as handle:
        writer.write(handle)

    with pytest.raises(WorkspaceError, match="OCR.*nicht aktiviert"):
        _workspace(tmp_path).read_file("scan.pdf")


def test_read_file_rejects_encrypted_pdf(tmp_path) -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.encrypt("secret")
    with (tmp_path / "encrypted.pdf").open("wb") as handle:
        writer.write(handle)

    with pytest.raises(WorkspaceError, match="Verschlüsselte PDF"):
        _workspace(tmp_path).read_file("encrypted.pdf")


def test_read_file_enforces_pdf_file_size_limit(tmp_path, monkeypatch) -> None:
    _write_text_pdf(tmp_path / "large.pdf")
    monkeypatch.setattr(os_operations, "MAX_PDF_FILE_BYTES", 10)

    with pytest.raises(WorkspaceError, match="Leselimit"):
        _workspace(tmp_path).read_file("large.pdf")


def test_read_file_enforces_pdf_page_limit(tmp_path, monkeypatch) -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_blank_page(width=200, height=200)
    with (tmp_path / "many-pages.pdf").open("wb") as handle:
        writer.write(handle)
    monkeypatch.setattr(os_operations, "MAX_PDF_PAGES", 1)

    with pytest.raises(WorkspaceError, match="Seitenlimit"):
        _workspace(tmp_path).read_file("many-pages.pdf")


def test_read_file_enforces_pdf_text_limit(tmp_path, monkeypatch) -> None:
    _write_text_pdf(tmp_path / "verbose.pdf", "Long text for the PDF")
    monkeypatch.setattr(os_operations, "MAX_PDF_TEXT_CHARS", 10)

    with pytest.raises(WorkspaceError, match="Extrahierter PDF-Text"):
        _workspace(tmp_path).read_file("verbose.pdf")


def test_write_file_does_not_treat_pdf_as_text(tmp_path) -> None:
    with pytest.raises(WorkspaceError, match="nicht als Text geschrieben"):
        _workspace(tmp_path).write_file("document.pdf", "not a pdf")


@pytest.mark.parametrize("filename", [".env", ".env.production", "credentials.json", "server.pem"])
def test_read_file_rejects_sensitive_files(tmp_path, filename) -> None:
    (tmp_path / filename).write_text("secret", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="Secret-/Credential"):
        _workspace(tmp_path).read_file(filename)


def test_read_file_rejects_oversized_text(tmp_path) -> None:
    (tmp_path / "large.txt").write_text("x" * 1_000_001, encoding="utf-8")
    with pytest.raises(WorkspaceError, match="Leselimit"):
        _workspace(tmp_path).read_file("large.txt")


def test_copy_file_rejects_sensitive_source(tmp_path) -> None:
    (tmp_path / "credentials.json").write_text("secret", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="Secret-/Credential"):
        _workspace(tmp_path).copy_file("credentials.json", "copy.txt")


@pytest.mark.parametrize("relative_path", [".git/config", ".cli-agent/history.json", "service.log"])
def test_read_file_rejects_sensitive_project_artifacts(tmp_path, relative_path) -> None:
    file_path = tmp_path / relative_path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text("sensitive", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="Secret-/Credential"):
        _workspace(tmp_path).read_file(relative_path)


@pytest.mark.parametrize("path", [r"C:\outside.txt", r"D:relative.txt"])
def test_workspace_rejects_windows_drive_paths(tmp_path, path: str) -> None:
    with pytest.raises(WorkspaceError, match="relativ"):
        _workspace(tmp_path).read_file(path)


@pytest.mark.parametrize("relative_path", [".env", ".env.local", ".git/config", ".cli-agent/history.json", "server.pem", "service.log"])
def test_write_file_rejects_sensitive_destinations(tmp_path, relative_path) -> None:
    path = tmp_path / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with pytest.raises(WorkspaceError, match="Ändern"):
        _workspace(tmp_path).write_file(relative_path, "replacement")
    assert not path.exists()


@pytest.mark.parametrize("relative_path", [".env", ".git/HEAD", ".cli-agent/history.json", "service.log"])
def test_delete_file_rejects_sensitive_files(tmp_path, relative_path) -> None:
    path = tmp_path / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("original", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="Ändern"):
        _workspace(tmp_path).delete_file(relative_path)
    assert path.read_text(encoding="utf-8") == "original"


@pytest.mark.parametrize("destination", [".env", ".git/config", ".cli-agent/result.txt", "service.log"])
def test_copy_file_rejects_sensitive_destinations(tmp_path, destination) -> None:
    (tmp_path / "source.txt").write_text("safe", encoding="utf-8")
    path = tmp_path / destination
    path.parent.mkdir(parents=True, exist_ok=True)
    with pytest.raises(WorkspaceError, match="Ändern"):
        _workspace(tmp_path).copy_file("source.txt", destination)
    assert not path.exists()


@pytest.mark.parametrize("directory", [".git", ".cli-agent", ".ssh", ".aws"])
def test_make_directory_rejects_sensitive_directories(tmp_path, directory) -> None:
    with pytest.raises(WorkspaceError, match="Ändern"):
        _workspace(tmp_path).make_directory(directory)
    assert not (tmp_path / directory).exists()
