from pathlib import Path
import pytest
import os
from cli_agent.workflows.flow_filesystem_operations import inventory_directory
from cli_agent.workflows.flow import load_flow

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


def test_inventory_directory_expands_root_var(tmp_path):
    flow = tmp_path / "flow.toml"
    flow.write_text(
        'version = 1\n[vars]\nokf_root = "docs"\n'
        '[[steps]]\nid = "inventory"\noutput = "state/inventory.json"\n'
        '[steps.filesystem_operation]\nkind = "inventory"\n'
        'directory = "${var.okf_root}"\n',
        encoding="utf-8",
    )
    parsed = load_flow(flow, workspace=tmp_path)
    assert parsed.steps[0].filesystem_operation["directory"] == "docs"


@pytest.mark.parametrize("value", ["../outside", "docs/../other"])
def test_inventory_directory_rejects_unsafe_root_var(tmp_path, value):
    flow = tmp_path / "flow.toml"
    flow.write_text(
        'version = 1\n[vars]\nokf_root = "' + value + '"\n'
        '[[steps]]\nid = "inventory"\noutput = "state/inventory.json"\n'
        '[steps.filesystem_operation]\nkind = "inventory"\n'
        'directory = "${var.okf_root}"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_flow(flow, workspace=tmp_path)


def test_inventory_directory_rejects_unknown_var(tmp_path):
    flow = tmp_path / "flow.toml"
    flow.write_text(
        'version = 1\n[[steps]]\nid = "inventory"\n'
        'output = "state/inventory.json"\n'
        '[steps.filesystem_operation]\nkind = "inventory"\n'
        'directory = "${var.missing}"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Inventory-Variable"):
        load_flow(flow, workspace=tmp_path)


def test_inventory_directory_rejects_platform_absolute_root_var(tmp_path):
    absolute = str((tmp_path / "outside").resolve())
    flow = tmp_path / "flow-absolute.toml"
    flow.write_text(
        'version = 1\n[vars]\nokf_root = "' + absolute.replace('\\', '\\\\') + '"\n'
        '[[steps]]\nid = "inventory"\noutput = "state/inventory.json"\n'
        '[steps.filesystem_operation]\nkind = "inventory"\n'
        'directory = "${var.okf_root}"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_flow(flow, workspace=tmp_path)


def test_inventory_stable_slugs_distinguish_ambiguous_paths(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "b.md").write_text("one", encoding="utf-8")
    (tmp_path / "a-b.md").write_text("two", encoding="utf-8")
    result = inventory_directory(tmp_path, ".", recursive=True, extensions=(".md",), stable_slugs=True)
    by_path = {item["path"]: item["slug"] for item in result["files"]}
    assert len(by_path) == 2
    assert len(set(by_path.values())) == 2
    assert all(len(slug) <= 77 for slug in by_path.values())
    assert by_path == {item["path"]: item["slug"] for item in inventory_directory(
        tmp_path, ".", recursive=True, extensions=(".md",), stable_slugs=True
    )["files"]}
    assert all("slug" not in item for item in inventory_directory(
        tmp_path, ".", recursive=True, extensions=(".md",)
    )["files"])


def test_inventory_stable_slugs_rejects_non_boolean(tmp_path):
    with pytest.raises(ValueError, match="stable_slugs"):
        inventory_directory(tmp_path, ".", stable_slugs="true")


def test_flow_accepts_stable_slug_inventory_option(tmp_path):
    flow = tmp_path / "flow.toml"
    flow.write_text(
        'version = 1\n[[steps]]\nid = "inventory"\n'
        'output = "state/inventory.json"\n'
        '[steps.filesystem_operation]\nkind = "inventory"\n'
        'directory = "."\nstable_slugs = true\n',
        encoding="utf-8",
    )
    parsed = load_flow(flow, workspace=tmp_path)
    assert parsed.steps[0].filesystem_operation["stable_slugs"] is True
