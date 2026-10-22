import pytest
from cli_agent.flow_json_validation import validate_okf_plan


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
