from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from cli_agent.okf_mcp_server.repository import OkfRepository
from cli_agent.okf_mcp_server.repository_support import (
    OkfRepositoryError,
    RepositoryMetadataMixin,
)
from cli_agent.okf_mcp_server.workspace import WorkspacePathError, WorkspaceRoot


def _concept(*, title: str = "Example", extra: str = "") -> str:
    return (
        "---\n"
        "type: concept\n"
        f"title: {title}\n"
        f"{extra}"
        "---\n"
        "Body\n"
    )


def test_workspace_rejects_unsafe_paths(tmp_path: Path) -> None:
    workspace = WorkspaceRoot.from_directory(tmp_path)
    for path in ("../outside.md", "/tmp/outside.md", "C:\\outside.md", ""):
        with pytest.raises(WorkspacePathError):
            workspace.resolve(path)


def test_workspace_rejects_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "secret.md").write_text("secret", encoding="utf-8")
    link = tmp_path / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available in this test environment")

    workspace = WorkspaceRoot.from_directory(tmp_path)
    with pytest.raises(WorkspacePathError, match="außerhalb"):
        workspace.resolve("escape/secret.md")


def test_repository_validates_limits_and_directory(tmp_path: Path) -> None:
    with pytest.raises(OkfRepositoryError, match="max_read_bytes"):
        OkfRepository.from_directory(tmp_path, max_read_bytes=0)
    with pytest.raises(OkfRepositoryError, match="max_index_entries"):
        OkfRepository.from_directory(tmp_path, max_index_entries=0)
    with pytest.raises(OkfRepositoryError):
        OkfRepository.from_directory(tmp_path / "missing")


def test_index_md_is_used_and_only_internal_safe_links_are_exposed(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (tmp_path / "concept.md").write_text(_concept(), encoding="utf-8")
    (tmp_path / "index.md").write_text(
        "[Concept](concept.md)\n"
        "[Docs](docs)\n"
        "[Missing](missing.md)\n"
        "[External](https://example.org/x.md)\n"
        "[Escape](../outside.md)\n"
        "[Anchor](#section)\n",
        encoding="utf-8",
    )

    result = OkfRepository.from_directory(tmp_path).knowledge_index()

    assert result["source"] == "index.md"
    links = {link["label"]: link for link in result["internal_links"]}
    assert set(links) == {"Concept", "Docs", "Missing"}
    assert links["Concept"] == {
        "label": "Concept",
        "path": "concept.md",
        "kind": "document",
        "exists": True,
        "next_tool": "knowledge_read",
    }
    assert links["Docs"]["next_tool"] == "knowledge_index"
    assert links["Missing"]["exists"] is False


def test_synthesized_index_classifies_entries_and_respects_limit(tmp_path: Path) -> None:
    (tmp_path / "a-dir").mkdir()
    (tmp_path / "b.md").write_text(_concept(title="B"), encoding="utf-8")
    (tmp_path / "c.md").write_text("no frontmatter", encoding="utf-8")
    (tmp_path / "ignored.txt").write_text("ignored", encoding="utf-8")

    result = OkfRepository.from_directory(tmp_path, max_index_entries=2).knowledge_index()

    assert result["source"] == "synthesized"
    assert [entry["path"] for entry in result["entries"]] == ["a-dir", "b.md"]
    assert result["entries"][0]["next_tool"] == "knowledge_index"
    assert result["entries"][1]["title"] == "B"
    assert result["warnings"] == ["Index wurde nach 2 Einträgen abgeschnitten."]


def test_invalid_concept_remains_readable_with_warning(tmp_path: Path) -> None:
    (tmp_path / "broken.md").write_text("plain markdown", encoding="utf-8")
    repository = OkfRepository.from_directory(tmp_path)

    index = repository.knowledge_index()
    entry = index["entries"][0]
    assert entry["path"] == "broken.md"
    assert "warning" in entry

    result = repository.knowledge_read("broken.md")
    assert result["kind"] == "concept"
    assert result["summary"]["title"] == "broken"
    assert result["warning"] is not None
    assert result["content"] == "plain markdown"


def test_read_rejects_non_markdown_and_oversized_file(tmp_path: Path) -> None:
    (tmp_path / "data.txt").write_text("text", encoding="utf-8")
    (tmp_path / "large.md").write_text("12345", encoding="utf-8")
    repository = OkfRepository.from_directory(tmp_path, max_read_bytes=4)

    with pytest.raises(OkfRepositoryError, match="Markdown"):
        repository.knowledge_read("data.txt")
    with pytest.raises(OkfRepositoryError, match="größer"):
        repository.knowledge_read("large.md")


def test_read_rejects_invalid_utf8(tmp_path: Path) -> None:
    (tmp_path / "invalid.md").write_bytes(b"\xff\xfe")
    repository = OkfRepository.from_directory(tmp_path)

    with pytest.raises(OkfRepositoryError, match="UTF-8"):
        repository.knowledge_read("invalid.md")


def test_frontmatter_rejects_aliases_and_missing_type() -> None:
    with pytest.raises(OkfRepositoryError, match="aliases"):
        RepositoryMetadataMixin._parse_frontmatter(
            "---\ntype: concept\na: &x value\nb: *x\n---\n"
        )
    with pytest.raises(OkfRepositoryError, match="'type'"):
        RepositoryMetadataMixin._parse_frontmatter("---\ntitle: Missing type\n---\n")


def test_concept_summary_normalizes_metadata() -> None:
    summary = RepositoryMetadataMixin._concept_summary(
        "area/example.md",
        {
            "type": "concept",
            "description": "x" * 600,
            "tags": list(range(30)),
            "stale_after": date(2000, 1, 1),
            "verified": [{"by": "machine:test"}, {"by": "human:alice"}],
        },
    )

    assert summary["title"] == "example"
    assert len(summary["description"]) == 500
    assert summary["description"].endswith("...")
    assert len(summary["tags"]) == 20
    assert summary["stale_after"] == "2000-01-01"
    assert summary["is_stale"] is True
    assert summary["trust_tier"] == "human-reviewed"


def test_json_safe_converts_dates_tuples_and_unknown_objects() -> None:
    value = RepositoryMetadataMixin._json_safe(
        {"when": date(2026, 9, 12), "values": (1, object())}
    )
    assert value["when"] == "2026-09-12"
    assert value["values"][0] == 1
    assert isinstance(value["values"][1], str)
