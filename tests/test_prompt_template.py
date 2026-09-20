from __future__ import annotations

import pytest

from cli_agent.prompt_template import PromptTemplate, parse_variable_assignments


def test_template_collects_variables_in_first_occurrence_order() -> None:
    template = PromptTemplate.parse(
        "Hello {{var:name}}, project={{var:project}}, again={{var:name}}."
    )

    assert template.variables == ("name", "project")


def test_template_renders_values_once_without_recursive_expansion() -> None:
    template = PromptTemplate.parse("Hello {{var:name}}.")

    rendered = template.render({"name": "{{var:other}}"})

    assert rendered == "Hello {{var:other}}."


def test_template_preserves_multiline_and_special_value_content() -> None:
    template = PromptTemplate.parse("Value:\n{{var:value}}\nDone")

    rendered = template.render({"value": 'a=b\n"quoted"\\path'})

    assert rendered == 'Value:\na=b\n"quoted"\\path\nDone'


def test_escaped_placeholder_is_literal_and_not_a_variable() -> None:
    template = PromptTemplate.parse(r"Literal \{{var:name}}; real {{var:other}}")

    assert template.variables == ("other",)
    assert template.render({"other": "X"}) == "Literal {{var:name}}; real X"


@pytest.mark.parametrize(
    "source",
    [
        "{{var:}}",
        "{{var:1name}}",
        "{{var:has space}}",
        "{{var:name",
        r"\{{var:name",
    ],
)
def test_invalid_or_incomplete_placeholder_is_rejected(source: str) -> None:
    with pytest.raises(ValueError):
        PromptTemplate.parse(source)


def test_render_rejects_missing_and_unknown_values() -> None:
    template = PromptTemplate.parse("{{var:a}} {{var:b}}")

    with pytest.raises(ValueError, match="Fehlende"):
        template.render({"a": "one"})
    with pytest.raises(ValueError, match="Unbekannte"):
        template.render({"a": "one", "b": "two", "c": "three"})


def test_variable_assignments_split_only_first_equals() -> None:
    assert parse_variable_assignments(["query=a=b=c"]) == {"query": "a=b=c"}


def test_variable_assignments_reject_duplicates_and_invalid_syntax() -> None:
    with pytest.raises(ValueError, match="mehrfach"):
        parse_variable_assignments(["name=A", "name=B"])
    with pytest.raises(ValueError, match="NAME=WERT"):
        parse_variable_assignments(["name"])
    with pytest.raises(ValueError, match="Variablenname"):
        parse_variable_assignments(["1name=value"])


def test_large_plain_template_is_kept_as_one_literal_span() -> None:
    content = "x" * 1_000_000

    template = PromptTemplate.parse(content)

    assert template.parts == (content,)
    assert template.variables == ()


def test_many_placeholders_parse_without_suffix_rescans() -> None:
    content = "{{var:x}}" * 100_000

    template = PromptTemplate.parse(content)

    assert template.variables == ("x",)
    assert len(template.parts) == 100_000
    assert template.render({"x": "v"}) == "v" * 100_000
