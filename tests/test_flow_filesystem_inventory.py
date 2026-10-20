from pathlib import Path
import pytest
import os
from cli_agent.flow_filesystem_operations import inventory_directory
from cli_agent.flow import load_flow

@pytest.mark.skipif(os.name not in {"posix", "nt"}, reason="secure inventory requires POSIX or Windows")
def test_inventory_filtered_and_sorted(tmp_path):
    (tmp_path / "docs").mkdir()
    for name in ("b.md", "A.MD", "index.md", "other.txt"):
        (tmp_path / "docs" / name).write_text(name)
    (tmp_path / "docs" / "nested").mkdir()
    (tmp_path / "docs" / "nested" / "inner.md").write_text("x")
    result = inventory_directory(tmp_path, "docs", extensions=(".md",), exclude_names=("index.md",))
    assert [f["path"] for f in result["files"]] == ["docs/A.MD", "docs/b.md"]
    assert result["directories"] == [{"name": "nested", "path": "docs/nested"}]

@pytest.mark.skipif(os.name != "posix", reason="secure inventory requires POSIX dir_fd")
def test_inventory_recursive_and_budget(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "b").mkdir()
    (tmp_path / "a" / "b" / "x.txt").write_text("x")
    assert inventory_directory(tmp_path, "a", recursive=True)["file_count"] == 1
    with pytest.raises(ValueError, match="max_entries"):
        inventory_directory(tmp_path, "a", recursive=True, max_entries=1)

@pytest.mark.skipif(os.name != "posix", reason="secure inventory requires POSIX dir_fd")
def test_inventory_symlinks_and_traversal(tmp_path):
    (tmp_path / "a").mkdir()
    with pytest.raises(ValueError):
        inventory_directory(tmp_path, "../outside")
    with pytest.raises(ValueError):
        inventory_directory(tmp_path, "/tmp")
    try:
        (tmp_path / "a" / "link").symlink_to(tmp_path / "a", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    with pytest.raises(ValueError, match="Symlink"):
        inventory_directory(tmp_path, "a")
    with pytest.raises(ValueError, match="Symlink"):
        inventory_directory(tmp_path, "a/link")

def test_flow_parses_inventory(tmp_path):
    flow = tmp_path / "flow.toml"
    flow.write_text('version = 1\ninputs = ["folder"]\nresult = "steps.inventory.output"\n[[steps]]\nid = "inventory"\noutput = "state/inventory.json"\n[steps.filesystem_operation]\nkind = "inventory"\ndirectory = "\u0024{input.folder}"\nextensions = [".md"]\n')
    parsed = load_flow(flow, workspace=tmp_path)
    assert parsed.steps[0].filesystem_operation["kind"] == "inventory"
    assert parsed.steps[0].response_format == "json"

def test_flow_rejects_prompt_and_inventory(tmp_path):
    flow = tmp_path / "flow.toml"
    flow.write_text('version = 1\n[[steps]]\nid = "inventory"\nprompt_file = "prompt.md"\noutput = "state/inventory.json"\n[steps.filesystem_operation]\nkind = "inventory"\ndirectory = "."\n')
    with pytest.raises(ValueError, match="filesystem_operation"):
        load_flow(flow, workspace=tmp_path)


def test_inventory_supported_platforms(tmp_path):
    if os.name not in {"posix", "nt"}:
        pytest.skip("unsupported platform")
    (tmp_path / "docs").mkdir()
    assert inventory_directory(tmp_path, "docs")["file_count"] == 0
