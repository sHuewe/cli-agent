from __future__ import annotations

import json
from pathlib import Path

import pytest

from cli_agent.mcp_path_policy import read_path_policy, write_path_policy


def test_manifest_round_trip_for_many_windows_like_paths(tmp_path: Path) -> None:
    manifest = tmp_path / "paths.json"
    paths = {
        "protected_paths": ["C:\\secret\\admin.toml"],
        "mutation_protected_paths": [
            f"C:\\workspace\\checkpoints\\m002-c014-route-{i:05}.json"
            for i in range(800)
        ],
    }
    write_path_policy(manifest, paths)
    loaded = read_path_policy(manifest)
    assert tuple(map(str, loaded["protected_paths"])) == tuple(paths["protected_paths"])
    assert tuple(map(str, loaded["mutation_protected_paths"])) == tuple(
        paths["mutation_protected_paths"]
    )
    assert manifest.stat().st_size > 32767


def test_manifest_missing_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="nicht lesbar"):
        read_path_policy(tmp_path / "missing.json")


@pytest.mark.parametrize(
    "contents",
    [
        "not json",
        "{}",
        '{"protected_paths":"wrong","mutation_protected_paths":[]}',
        '{"protected_paths":[],"mutation_protected_paths":[42]}',
        '{"protected_paths":[],"mutation_protected_paths":[""]}',
    ],
)
def test_manifest_invalid_data_fails_closed(
    tmp_path: Path, contents: str,
) -> None:
    manifest = tmp_path / "invalid.json"
    manifest.write_text(contents, encoding="utf-8")
    with pytest.raises(ValueError, match="MCP-Pfad"):
        read_path_policy(manifest)


def test_manifest_rejects_oversize_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cli_agent.mcp_path_policy as module

    manifest = tmp_path / "large.json"
    manifest.write_text(json.dumps({
        "protected_paths": ["somewhere"],
        "mutation_protected_paths": [],
    }), encoding="utf-8")
    monkeypatch.setattr(module, "MAX_POLICY_BYTES", 8)
    with pytest.raises(ValueError, match="Größenlimit"):
        read_path_policy(manifest)


def test_manifest_cannot_replace_existing_file(tmp_path: Path) -> None:
    manifest = tmp_path / "paths.json"
    manifest.write_text("do not replace", encoding="utf-8")
    with pytest.raises(FileExistsError):
        write_path_policy(manifest, {
            "protected_paths": [],
            "mutation_protected_paths": [],
        })
    assert manifest.read_text(encoding="utf-8") == "do not replace"
