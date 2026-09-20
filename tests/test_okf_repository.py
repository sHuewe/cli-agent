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
    (docs / "nested.md").write_text(_concept(title="Nested"), encoding="utf-8")
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


def test_synthesized_subdirectory_index_classifies_entries_and_respects_limit(
    tmp_path: Path,
) -> None:
    area = tmp_path / "area"
    area.mkdir()
    (area / "a-dir").mkdir()
    (area / "b.md").write_text(_concept(title="B"), encoding="utf-8")
    (area / "c.md").write_text("no frontmatter", encoding="utf-8")
    (area / "ignored.txt").write_text("ignored", encoding="utf-8")
    (tmp_path / "index.md").write_text("[Area](area)\n", encoding="utf-8")

    repository = OkfRepository.from_directory(tmp_path, max_index_entries=2)
    result = repository.knowledge_index("area")

    assert result["source"] == "synthesized"
    assert [entry["path"] for entry in result["entries"]] == ["area/a-dir", "area/b.md"]
    assert result["entries"][0]["next_tool"] == "knowledge_index"
    assert result["entries"][1]["title"] == "B"
    assert result["warnings"] == [
        "Index-Prüfung wurde nach 2 Kandidaten abgeschnitten."
    ]


def test_synthesized_index_bounds_invalid_markdown_inspection(tmp_path: Path) -> None:
    area = tmp_path / "area"
    area.mkdir()
    for number in range(5):
        (area / f"{number:02d}-invalid.md").write_text(
            "plain markdown",
            encoding="utf-8",
        )
    (area / "99-valid.md").write_text(
        _concept(title="Late Valid"),
        encoding="utf-8",
    )
    (tmp_path / "index.md").write_text("[Area](area)\n", encoding="utf-8")

    repository = OkfRepository.from_directory(tmp_path, max_index_entries=3)
    result = repository.knowledge_index("area")

    assert result["entries"] == []
    assert result["warnings"] == [
        "Index-Prüfung wurde nach 3 Kandidaten abgeschnitten."
    ]


def test_repository_requires_index_md_directly_in_configured_root(tmp_path: Path) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "index.md").write_text("[Deep](deep.md)\n", encoding="utf-8")
    (nested / "deep.md").write_text(_concept(title="Deep"), encoding="utf-8")

    with pytest.raises(OkfRepositoryError, match="Repository-Root.*index.md"):
        OkfRepository.from_directory(tmp_path)


def test_root_index_does_not_expose_non_okf_markdown(tmp_path: Path) -> None:
    (tmp_path / "notes.md").write_text("confidential notes", encoding="utf-8")
    (tmp_path / "index.md").write_text("[Notes](notes.md)\n", encoding="utf-8")

    repository = OkfRepository.from_directory(tmp_path)
    index = repository.knowledge_index()

    assert index["source"] == "index.md"
    assert index["internal_links"] == []
    with pytest.raises(OkfRepositoryError, match="nicht konformes Markdown"):
        repository.knowledge_read("notes.md")


def test_non_conforming_markdown_is_hidden_inside_valid_okf_root(tmp_path: Path) -> None:
    (tmp_path / "valid.md").write_text(_concept(title="Valid"), encoding="utf-8")
    (tmp_path / "private-notes.md").write_text("confidential notes", encoding="utf-8")
    (tmp_path / "index.md").write_text(
        "[Valid](valid.md)\n[Private](private-notes.md)\n",
        encoding="utf-8",
    )
    repository = OkfRepository.from_directory(tmp_path)

    index = repository.knowledge_index()
    assert [link["path"] for link in index["internal_links"]] == ["valid.md"]

    with pytest.raises(OkfRepositoryError, match="nicht konformes Markdown"):
        repository.knowledge_read("private-notes.md")


def test_unlinked_unrelated_directory_is_not_discovered_from_root_index(
    tmp_path: Path,
) -> None:
    (tmp_path / "valid.md").write_text(_concept(title="Valid"), encoding="utf-8")
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    (unrelated / "notes.md").write_text("confidential notes", encoding="utf-8")
    (tmp_path / "index.md").write_text("[Valid](valid.md)\n", encoding="utf-8")
    repository = OkfRepository.from_directory(tmp_path)

    index = repository.knowledge_index()
    assert [link["path"] for link in index["internal_links"]] == ["valid.md"]


def test_read_rejects_non_markdown_and_oversized_file(tmp_path: Path) -> None:
    (tmp_path / "valid.md").write_text(_concept(title="V"), encoding="utf-8")
    (tmp_path / "index.md").write_text("[Valid](valid.md)\n", encoding="utf-8")
    (tmp_path / "data.txt").write_text("text", encoding="utf-8")
    (tmp_path / "large.md").write_text("x" * 101, encoding="utf-8")
    repository = OkfRepository.from_directory(tmp_path, max_read_bytes=100)

    with pytest.raises(OkfRepositoryError, match="Markdown"):
        repository.knowledge_read("data.txt")
    with pytest.raises(OkfRepositoryError, match="größer"):
        repository.knowledge_read("large.md")


def test_read_rejects_invalid_utf8(tmp_path: Path) -> None:
    (tmp_path / "valid.md").write_text(_concept(title="Valid"), encoding="utf-8")
    (tmp_path / "index.md").write_text("[Valid](valid.md)\n", encoding="utf-8")
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


def test_explicit_index_bounds_and_deduplicates_link_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "concept.md"
    target.write_text(_concept(title="Concept"), encoding="utf-8")
    (tmp_path / "index.md").write_text(
        "\n".join(
            [
                "[One](concept.md)",
                "[Two](concept.md)",
                "[Three](concept.md)",
                "[Four](concept.md)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    repository = OkfRepository.from_directory(tmp_path, max_index_entries=3)

    calls: list[Path] = []
    original = OkfRepository._is_okf_document

    def counting_is_okf_document(self: OkfRepository, path: Path) -> bool:
        calls.append(path)
        return original(self, path)

    monkeypatch.setattr(
        OkfRepository,
        "_is_okf_document",
        counting_is_okf_document,
    )

    result = repository.knowledge_index()

    assert [link["label"] for link in result["internal_links"]] == [
        "One",
        "Two",
        "Three",
    ]
    assert calls == [target.resolve()]
