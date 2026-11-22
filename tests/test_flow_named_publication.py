"""Named OKF publication preserves generator state and internal links."""
import json
from pathlib import Path

import pytest

from cli_agent.workflows.flow_named_publication import (
    publish_named_okf, validate_publication_report_path, _slug, _rewrite_markdown,
)


def workspace(tmp_path):
    root = tmp_path / "okf-batched-generated-v3"
    module = root / "doctrines/example"
    folder = module / "command"
    folder.mkdir(parents=True)
    group = tmp_path / "okf-batched/state-v3/docs/example/group.json"
    group.parent.mkdir(parents=True)
    group.write_text(json.dumps({
        "concepts": [
            {"id": "s001-c010", "title": "Befehlsgebung und Koordination",
             "path": "doctrines/example/command/s001-c010.md"},
            {"id": "s003-c002", "title": "Führungsgrundsätze",
             "path": "doctrines/example/command/s003-c002.md"},
        ]
    }), encoding="utf-8")
    (folder / "s001-c010.md").write_text(
        "---\ntype: concept\ntitle: Befehlsgebung und Koordination\n---\n"
        "[Other](s003-c002.md#details) [Index](index.md)\n"
        "\x60[sample](s003-c002.md)\x60\n",
        encoding="utf-8",
    )
    (folder / "s003-c002.md").write_text(
        "---\ntype: concept\ntitle: Führungsgrundsätze\n---\n"
        "[Back](s001-c010.md)\n",
        encoding="utf-8",
    )
    (folder / "index.md").write_text(
        "# Command\n"
        "- [Commands](s001-c010.md)\n"
        "- [Principles](s003-c002.md)\n",
        encoding="utf-8",
    )
    (module / "index.md").write_text(
        "[Command](command/index.md)\n",
        encoding="utf-8",
    )
    (root / "index.md").write_text(
        "[Doctrines](doctrines/example/index.md)\n"
        "[Theme](themen/strategy.md)\n",
        encoding="utf-8",
    )
    (root / "themen").mkdir()
    (root / "themen/strategy.md").write_text(
        "[Commands](../doctrines/example/command/s001-c010.md)\n",
        encoding="utf-8",
    )
    return root, group


def publish(tmp_path):
    return publish_named_okf(
        workspace=tmp_path,
        source_root="okf-batched-generated-v3",
        destination_root="okf-batched-published-v3",
        state_root="okf-batched/state-v3/docs",
    )


def test_publish_renames_concepts_and_rewrites_all_local_navigation(tmp_path):
    root, group = workspace(tmp_path)
    before = group.read_bytes()
    original = (root / "doctrines/example/command/s001-c010.md").read_bytes()
    result = publish(tmp_path)
    published = tmp_path / "okf-batched-published-v3"
    folder = published / "doctrines/example/command"
    assert result["concepts_renamed"] == 2
    commands = folder / "befehlsgebung-und-koordination.md"
    principles = folder / "fuehrungsgrundsaetze.md"
    assert commands.is_file() and principles.is_file()
    assert not (folder / "s001-c010.md").exists()
    index = (folder / "index.md").read_text(encoding="utf-8")
    assert "(befehlsgebung-und-koordination.md)" in index
    assert "(fuehrungsgrundsaetze.md)" in index
    contents = commands.read_text(encoding="utf-8")
    assert "(fuehrungsgrundsaetze.md#details)" in contents
    assert "\x60[sample](s003-c002.md)\x60" in contents
    assert "../doctrines/example/command/befehlsgebung-und-koordination.md" in (
        published / "themen/strategy.md"
    ).read_text(encoding="utf-8")
    assert group.read_bytes() == before
    assert (root / "doctrines/example/command/s001-c010.md").read_bytes() == original
    mapping = json.loads((published / "concept-path-map.json").read_text(encoding="utf-8"))
    assert len(mapping["concepts"]) == 2


def test_publish_replaces_prior_tree_without_changing_sources(tmp_path):
    root, group = workspace(tmp_path)
    assert publish(tmp_path)["status"] == "ok"
    assert publish(tmp_path)["status"] == "ok"
    assert not (tmp_path / ".okf-batched-published-v3.previous").exists()
    assert (root / "doctrines/example/command/s001-c010.md").is_file()


def test_publish_fails_closed_if_internal_link_is_missing(tmp_path):
    root, _ = workspace(tmp_path)
    path = root / "doctrines/example/command/index.md"
    path.write_text("[Missing](ghost.md)\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Broken internal Markdown link"):
        publish(tmp_path)
    assert not (tmp_path / "okf-batched-published-v3").exists()


def test_publish_does_not_replace_previous_output_on_validation_failure(tmp_path):
    root, _ = workspace(tmp_path)
    publish(tmp_path)
    published = tmp_path / "okf-batched-published-v3"
    prior = (published / "concept-path-map.json").read_bytes()
    (root / "themen/strategy.md").write_text(
        "[Missing](absent.md)", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="Broken internal Markdown link"):
        publish(tmp_path)
    assert (published / "concept-path-map.json").read_bytes() == prior


def test_publish_rejects_symlinked_source_and_duplicate_targets(tmp_path):
    root, group = workspace(tmp_path)
    external = tmp_path / "external.md"
    external.write_text("external", encoding="utf-8")
    path = root / "doctrines/example/command/attack.md"
    try:
        path.symlink_to(external)
    except (NotImplementedError, OSError):
        pytest.skip("Symlink unavailable")
    with pytest.raises(ValueError, match="Symlink"):
        publish(tmp_path)
    assert external.read_text(encoding="utf-8") == "external"


def test_same_title_does_not_overwrite_concepts(tmp_path):
    root, group = workspace(tmp_path)
    original = json.loads(group.read_text(encoding="utf-8"))
    original["concepts"][1]["title"] = "Befehlsgebung und Koordination"
    group.write_text(json.dumps(original), encoding="utf-8")
    path = root / "doctrines/example/command/s003-c002.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "Führungsgrundsätze", "Befehlsgebung und Koordination"
        ), encoding="utf-8",
    )
    publish(tmp_path)
    folder = tmp_path / "okf-batched-published-v3/doctrines/example/command"
    assert (folder / "befehlsgebung-und-koordination.md").exists()
    assert (folder / "befehlsgebung-und-koordination-s003-c002.md").exists()


def test_slug_transliterates_german_characters():
    assert _slug("Äußere Überwachung & Befehle") == "aeussere-ueberwachung-befehle"


def test_named_publication_filesystem_operation_is_accepted_by_flow_loader(tmp_path):
    from cli_agent.workflows.flow import load_flow

    config = tmp_path / "publish.toml"
    config.write_text(
        'version = 1\n'
        'result = "steps.publish.output"\n'
        '[[steps]]\n'
        'id = "publish"\n'
        'output = "state/publish.json"\n'
        'overwrite_output = true\n'
        '[steps.filesystem_operation]\n'
        'kind = "publish_named_okf"\n'
        'source_root = "generated"\n'
        'destination_root = "published"\n'
        'state_root = "state/docs"\n',
        encoding="utf-8",
    )
    flow = load_flow(Path("publish.toml"), workspace=tmp_path)
    assert flow.steps[0].filesystem_operation["kind"] == "publish_named_okf"


def test_named_publication_operation_requires_checkpoint_overwrite(tmp_path):
    from cli_agent.workflows.flow import load_flow

    config = tmp_path / "publish.toml"
    config.write_text(
        'version = 1\n'
        'result = "steps.publish.output"\n'
        '[[steps]]\n'
        'id = "publish"\n'
        'output = "state/publish.json"\n'
        '[steps.filesystem_operation]\n'
        'kind = "publish_named_okf"\n'
        'source_root = "generated"\n'
        'destination_root = "published"\n'
        'state_root = "state/docs"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="overwrite_output"):
        load_flow(Path("publish.toml"), workspace=tmp_path)


def test_publish_rejects_links_escaping_okf_root(tmp_path):
    root, _ = workspace(tmp_path)
    path = root / "doctrines/example/command/index.md"
    path.write_text("[Outside](../../../../secret.md)\n", encoding="utf-8")
    with pytest.raises(ValueError, match="escapes the published root"):
        publish(tmp_path)
    assert not (tmp_path / "okf-batched-published-v3").exists()


def test_publish_rewrites_multiline_markdown_links(tmp_path):
    root, _ = workspace(tmp_path)
    path = root / "doctrines/example/command/index.md"
    path.write_text(
        "[First](\ns001-c010.md\n)\n"
        "[Second][key]\n\n[key]:\n  s003-c002.md\n"
        "\x60[unchanged](s001-c010.md)\x60\n"
        "~~~\n[code](s001-c010.md)\n~~~\n",
        encoding="utf-8",
    )
    publish(tmp_path)
    published = (
        tmp_path / "okf-batched-published-v3/doctrines/example/command/index.md"
    ).read_text(encoding="utf-8")
    assert "[First](\nbefehlsgebung-und-koordination.md\n)" in published
    assert "[key]:\n  fuehrungsgrundsaetze.md" in published
    assert "[Second][key]" in published
    assert "\x60[unchanged](s001-c010.md)\x60" in published
    assert "[code](s001-c010.md)" in published


@pytest.mark.parametrize("report_path", [
    "okf-batched-generated-v3/doctrines/example/command/s001-c010.md",
    "okf-batched/state-v3/docs/example/group.json",
    "okf-batched-published-v3/index.md",
])
def test_publication_report_cannot_overwrite_source_or_state(tmp_path, report_path):
    root, _ = workspace(tmp_path)
    with pytest.raises(ValueError, match="outside all publication"):
        validate_publication_report_path(
            workspace=tmp_path, output=tmp_path / report_path,
            source_root="okf-batched-generated-v3",
            destination_root="okf-batched-published-v3",
            state_root="okf-batched/state-v3/docs",
        )

def test_publish_preserves_code_span_with_shorter_backtick_runs(tmp_path):
    root, _ = workspace(tmp_path)
    page = root / "doctrines/example/command/index.md"
    page.write_text(
        "``example `tick` [sample](ghost.md)``\n"
        "``example `tick` [existing](s001-c010.md)``\n"
        "[Real](s001-c010.md)\n",
        encoding="utf-8",
    )
    publish(tmp_path)
    output = (
        tmp_path / "okf-batched-published-v3/doctrines/example/command/index.md"
    ).read_text(encoding="utf-8")
    assert "``example `tick` [sample](ghost.md)``" in output
    assert "``example `tick` [existing](s001-c010.md)``" in output
    assert "[Real](befehlsgebung-und-koordination.md)" in output


def test_publish_preserves_longer_code_spans_with_nested_shorter_runs(tmp_path):
    root, _ = workspace(tmp_path)
    page = root / "doctrines/example/command/index.md"
    page.write_text(
        "```Example ``nested`` `tick` [sample](ghost.md)```\n"
        "[Actual](s003-c002.md)\n",
        encoding="utf-8",
    )
    publish(tmp_path)
    output = (
        tmp_path / "okf-batched-published-v3/doctrines/example/command/index.md"
    ).read_text(encoding="utf-8")
    assert "[sample](ghost.md)" in output
    assert "[Actual](fuehrungsgrundsaetze.md)" in output
