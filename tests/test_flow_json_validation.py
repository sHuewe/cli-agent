import pytest
from cli_agent.flow_json_validation import validate_okf_plan, build_okf_manifest


def doctrine(concepts=None):
    return {"status":"ok", "folder":{"id":"source-123","path":"doctrines/source-123"},
            "folders":[], "concepts": concepts or [
                {"id":"first","path":"doctrines/source-123/first.md"}]}


def test_doctrine_validation_rejects_duplicate_ids():
    with pytest.raises(ValueError,match="doppelt"):
        validate_okf_plan(doctrine([
            {"id":"same","path":"doctrines/source-123/one.md"},
            {"id":"same","path":"doctrines/source-123/two.md"}]), plan_kind="doctrine")


@pytest.mark.parametrize("path", [
    "../README.md", "themen/index.md", "themen/log.md", "/tmp/x.md",
    "themen/other.md", "themen/../outside.md", "themen\\safe.md",
])
def test_theme_paths_fail_closed(path):
    data={"status":"ok","themes":[{"id":"safe","path":path,
          "concept_paths":["doctrines/first/a.md","doctrines/second/b.md"]}]}
    with pytest.raises(ValueError):
        validate_okf_plan(data,plan_kind="themes")


def test_valid_and_noop_theme_plan():
    data={"status":"ok","themes":[{"id":"safe","path":"themen/safe.md",
        "concept_paths":["doctrines/first/a.md","doctrines/second/b.md"]}]}
    assert validate_okf_plan(data,plan_kind="themes")==data
    assert validate_okf_plan({"status":"ok","themes":[]},plan_kind="themes")["themes"]==[]


def test_invalid_plan_status_blocks_writing():
    with pytest.raises(ValueError,match="nicht erfolgreich"):
        validate_okf_plan({"status":"needs_attention","themes":[]},plan_kind="themes")


@pytest.mark.parametrize("path", ["doctrines/source-123/index.md",
                                    "doctrines/source-123/log.md",
                                    "doctrines/other/first.md"])
def test_doctrine_rejects_reserved_or_wrong_paths(path):
    with pytest.raises(ValueError):
        validate_okf_plan(doctrine([{"id":"first","path":path}]),plan_kind="doctrine")


def test_complete_manifest_preserves_each_planned_concept_exactly_once():
    plan = doctrine([
        {"id": "first", "path": "doctrines/source-123/first.md",
         "title": "First", "description": "One", "tags": ["alpha"]},
        {"id": "second", "path": "doctrines/source-123/second.md",
         "title": "Second", "description": "Two", "tags": ["beta"]},
    ])
    plan["folder"].update(title="Source", description="Source overview")
    plan["source_path"] = "doctrines/source.md"
    navigation = {
        "status": "ok", "module_path": "doctrines/source-123",
        "index_paths": ["doctrines/source-123/index.md"], "warnings": [],
    }
    result = build_okf_manifest(plan=plan, navigation=navigation, expected_source_path="doctrines/source.md")
    assert result["status"] == "ok"
    assert [x["path"] for x in result["concepts"]] == [
        "doctrines/source-123/first.md", "doctrines/source-123/second.md"
    ]
    assert result["concepts"][1]["tags"] == ["beta"]
    assert build_okf_manifest(plan=plan, expected_source_path="doctrines/source.md", navigation={
        **navigation, "status": "needs_attention", "warnings": ["missing"]
    })["concepts"] == []
    with pytest.raises(ValueError, match="index_paths"):
        build_okf_manifest(plan=plan, expected_source_path="doctrines/source.md", navigation={**navigation, "index_paths": []})


@pytest.mark.parametrize("source", [
    "../secrets.md", "../../secrets.md", "/absolute/file.md",
    "C:/secrets.md", "doctrines/./source.md", "doctrines/../source.md",
    "doctrines//source.md", "doctrines/source.md ", "doctrines\\\\source.md",
])
def test_manifest_rejects_invalid_source_path(source):
    plan = doctrine([{"id": "first", "path": "doctrines/source-123/first.md",
                     "title": "First", "description": "One"}])
    plan["folder"].update(title="Source", description="Overview")
    plan["source_path"] = source
    nav = {"status": "ok", "module_path": "doctrines/source-123",
           "index_paths": ["doctrines/source-123/index.md"]}
    with pytest.raises(ValueError, match="source_path"):
        build_okf_manifest(plan=plan, navigation=nav,
                           expected_source_path="doctrines/source.md")


def test_manifest_rejects_valid_but_wrong_source():
    plan = doctrine([{"id": "first", "path": "doctrines/source-123/first.md",
                     "title": "First", "description": "One"}])
    plan["folder"].update(title="Source", description="Overview")
    plan["source_path"] = "doctrines/other.md"
    nav = {"status": "ok", "module_path": "doctrines/source-123",
           "index_paths": ["doctrines/source-123/index.md"]}
    with pytest.raises(ValueError, match="inventarisierten"):
        build_okf_manifest(plan=plan, navigation=nav,
                           expected_source_path="doctrines/source.md")
