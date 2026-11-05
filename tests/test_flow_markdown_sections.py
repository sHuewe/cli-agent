"""Deterministic Markdown section planner contracts."""
from pathlib import Path
import pytest

from cli_agent.workflows.flow_filesystem_operations import markdown_sections
from cli_agent.workflows.flow import load_flow


def test_short_markdown_uses_direct_mode(tmp_path: Path) -> None:
    (tmp_path / "doc.md").write_text("# Heading\nsmall\n", encoding="utf-8")
    result = markdown_sections(tmp_path, "doc.md", target_chars=4000)
    assert result["mode"] == "direct"
    assert result["sections"] == []


def test_long_sections_cover_every_line(tmp_path: Path) -> None:
    content = "".join(
        f"# Chapter {n}\n" + ("One paragraph with details.\n" * 100)
        for n in range(8)
    )
    path = tmp_path / "doc.md"
    path.write_text(content, encoding="utf-8")
    result = markdown_sections(tmp_path, "doc.md", target_chars=4000)
    assert result["mode"] == "sections"
    sections = result["sections"]
    assert len(sections) >= 2
    assert sections[0]["start_line"] == 1
    assert sections[-1]["end_line"] == len(content.splitlines())
    assert all(
        a["end_line"] + 1 == b["start_line"]
        for a, b in zip(sections, sections[1:])
    )
    assert [s["id"] for s in sections] == [
        f"s{i:03d}" for i in range(1, len(sections) + 1)
    ]


def test_rejects_unsafe_paths(tmp_path: Path) -> None:
    for path in ("../secret.md", "/tmp/file.md", "secret.txt"):
        with pytest.raises(ValueError):
            markdown_sections(tmp_path, path)


def test_rejects_section_explosion(tmp_path: Path) -> None:
    (tmp_path / "doc.md").write_text("a" * 1000 + "\n" + ("abc\n" * 12000))
    with pytest.raises(ValueError, match="markdown_sections: too many sections; increase target_chars\\."):
        markdown_sections(tmp_path, "doc.md", target_chars=4000, max_sections=2)


def test_flow_accepts_markdown_sections(tmp_path: Path) -> None:
    flow_path = tmp_path / "flow.toml"
    flow_path.write_text(
        'version = 1\ninputs = ["source"]\n'
        '[[steps]]\nid = "sections"\noutput = "state/sections.json"\n'
        '[steps.filesystem_operation]\nkind = "markdown_sections"\n'
        'source_path = "${input.source}"\ntarget_chars = 5000\n',
        encoding="utf-8",
    )
    flow = load_flow(flow_path, workspace=tmp_path)
    assert flow.steps[0].filesystem_operation["kind"] == "markdown_sections"


@pytest.mark.parametrize("source, output", [
    ("doc.md", "doc.md"),
    ("doc.md", "folder/../doc.md"),
])
def test_section_output_cannot_replace_source(
    tmp_path: Path, source: str, output: str,
) -> None:
    from cli_agent.workflows import flow as flow_module
    (tmp_path / "doc.md").write_text("# Important source\n", encoding="utf-8")
    (tmp_path / "folder").mkdir()
    (tmp_path / "flow.toml").write_text(
        'version = 1\n[[steps]]\nid = "sections"\n'
        f'output = "{output}"\noverwrite_output = true\n'
        '[steps.filesystem_operation]\nkind = "markdown_sections"\n'
        f'source_path = "{source}"\n',
        encoding="utf-8",
    )
    step = load_flow(tmp_path / "flow.toml", workspace=tmp_path).steps[0]
    with pytest.raises(ValueError, match="markdown_sections\\.output must not overwrite the Markdown source file must n"):
        flow_module._reject_markdown_source_output_alias(
            step, workspace=tmp_path, flow_input={},
            output=tmp_path / "doc.md",
        )
    assert (tmp_path / "doc.md").read_text(encoding="utf-8") == "# Important source\n"


def test_sensitive_markdown_sources_are_rejected_before_read(tmp_path: Path) -> None:
    from cli_agent.workflows.flow_filesystem_operations import markdown_sections
    (tmp_path / "secrets").mkdir()
    private = tmp_path / "secrets" / "doc.md"
    private.write_text("# confidential-heading\n" + ("a\n" * 6000), encoding="utf-8")
    with pytest.raises(ValueError, match="markdown_sections\\.source_path ist als Secret-/Credential- oder protected in"):
        markdown_sections(tmp_path, "secrets/doc.md", target_chars=4000)


def test_many_headings_preserve_full_line_coverage(tmp_path: Path) -> None:
    from cli_agent.workflows.flow_filesystem_operations import markdown_sections
    source = tmp_path / "headings.md"
    source.write_text("# x\n" * 50000, encoding="utf-8")
    result = markdown_sections(tmp_path, "headings.md", target_chars=100000)
    sections = result["sections"]
    assert result["mode"] == "sections"
    assert sections[0]["start_line"] == 1
    assert sections[-1]["end_line"] == 50000
    assert all(a["end_line"] + 1 == b["start_line"] for a, b in zip(sections, sections[1:]))
