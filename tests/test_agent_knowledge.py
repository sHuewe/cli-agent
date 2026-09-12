from __future__ import annotations

import json

import pytest

from cli_agent.agent_knowledge import (
    _KnowledgeRunState,
    _assemble_knowledge_payload,
    _fallback_knowledge_selection,
    _validate_knowledge_selection,
)


def _state_with_concept() -> tuple[_KnowledgeRunState, str]:
    state = _KnowledgeRunState()
    token = state.register_concept(
        {
            "path": "domain/concept.md",
            "kind": "concept",
            "content": "Knowledge",
            "warning": "stale",
        }
    )
    assert token is not None
    return state, token


def test_register_concept_is_stable_and_ignores_non_concepts() -> None:
    state = _KnowledgeRunState()
    assert state.register_concept({"path": "index.md", "kind": "index"}) is None

    document = {"path": "a.md", "kind": "concept", "content": "A"}
    first = state.register_concept(document)
    second = state.register_concept(document)

    assert first is not None
    assert first == second
    assert first.startswith("OKFSEL-")
    assert state.concepts[first] == document


def test_add_allowed_calls_merges_tool_sets() -> None:
    state = _KnowledgeRunState()
    state.add_allowed_calls({"a": {"knowledge_read"}})
    state.add_allowed_calls({"a": {"knowledge_index"}, "b": {"knowledge_read"}})
    assert state.allowed_calls == {
        "a": {"knowledge_read", "knowledge_index"},
        "b": {"knowledge_read"},
    }


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("not json", "kein gültiges JSON"),
        ("[]", "kein JSON-Objekt"),
        (json.dumps({"found_content": "yes", "selected_okf_tokens": []}), "boolescher"),
        (json.dumps({"found_content": True, "selected_okf_tokens": "x"}), "Liste von Zeichenketten"),
        (json.dumps({"found_content": False, "selected_okf_tokens": [], "warnings": "x"}), "warnings"),
        (json.dumps({"found_content": True, "selected_okf_tokens": []}), "fehlt"),
        (json.dumps({"found_content": False, "selected_okf_tokens": ["x"]}), "muss `selected_okf_tokens` leer"),
        (json.dumps({"found_content": False, "selected_okf_tokens": [], "reason_code": "other"}), "reason_code"),
    ],
)
def test_validate_knowledge_selection_rejects_invalid_shapes(answer: str, expected: str) -> None:
    _selection, error = _validate_knowledge_selection(answer, _KnowledgeRunState())
    assert error is not None
    assert expected in error


def test_validate_selection_accepts_known_token_and_rejects_unknown_token() -> None:
    state, token = _state_with_concept()
    valid = json.dumps(
        {
            "found_content": True,
            "selected_okf_tokens": [token],
            "warnings": [],
        }
    )
    selection, error = _validate_knowledge_selection(valid, state)
    assert error is None
    assert selection is not None

    invalid = json.dumps(
        {
            "found_content": True,
            "selected_okf_tokens": ["OKFSEL-UNKNOWN"],
            "warnings": [],
        }
    )
    _selection, error = _validate_knowledge_selection(invalid, state)
    assert error is not None
    assert "keinem erfolgreich gelesenen Concept" in error


def test_validate_not_applicable_only_before_followup_and_not_found_requires_concept() -> None:
    state = _KnowledgeRunState()
    not_applicable = json.dumps(
        {
            "found_content": False,
            "selected_okf_tokens": [],
            "reason_code": "not_applicable",
        }
    )
    assert _validate_knowledge_selection(not_applicable, state)[1] is None

    state.successful_followup_calls = 1
    assert "nur vor Beginn" in (_validate_knowledge_selection(not_applicable, state)[1] or "")

    empty = _KnowledgeRunState()
    not_found = json.dumps(
        {
            "found_content": False,
            "selected_okf_tokens": [],
            "reason_code": "not_found",
        }
    )
    assert "mindestens ein" in (_validate_knowledge_selection(not_found, empty)[1] or "")

    with_concept, _ = _state_with_concept()
    assert _validate_knowledge_selection(not_found, with_concept)[1] is None


def test_fallback_prefers_valid_tokens_from_invalid_response() -> None:
    state, token = _state_with_concept()
    fallback = _fallback_knowledge_selection(
        {
            "selected_okf_tokens": ["bad", token, token],
            "warnings": ["one", "one", 7],
        },
        state,
        reason="invalid",
    )
    assert fallback["found_content"] is True
    assert fallback["selected_okf_tokens"] == [token]
    assert fallback["warnings"] == ["one"]
    assert fallback["agent_fallback"]["strategy"] == "valid_tokens_from_invalid_response"


def test_fallback_uses_all_read_concepts_or_reports_incomplete_retrieval() -> None:
    state, token = _state_with_concept()
    fallback = _fallback_knowledge_selection(None, state, reason="empty")
    assert fallback["selected_okf_tokens"] == [token]
    assert fallback["agent_fallback"]["strategy"] == "all_read_concepts"

    empty = _fallback_knowledge_selection(None, _KnowledgeRunState(), reason="empty")
    assert empty["found_content"] is False
    assert empty["reason_code"] == "retrieval_incomplete"
    assert empty["agent_fallback"]["strategy"] == "no_read_concepts"


def test_assemble_payload_deduplicates_tokens_and_warnings() -> None:
    state, token = _state_with_concept()
    payload = _assemble_knowledge_payload(
        {
            "selected_okf_tokens": [token, token],
            "warnings": ["global", "global"],
        },
        state.concepts,
    )
    assert payload == {
        "found_content": True,
        "content": [
            {
                "concept": "domain/concept.md",
                "content_type": "full",
                "content": "Knowledge",
            }
        ],
        "warnings": ["global", "domain/concept.md: stale"],
    }


@pytest.mark.parametrize(
    "selection,match",
    [
        ({"selected_okf_tokens": []}, "keine ausgewählten"),
        ({"selected_okf_tokens": [7]}, "ungültigen OKF-Token"),
        ({"selected_okf_tokens": ["missing"]}, "unbekannte OKF-Tokens"),
        ({"selected_okf_tokens": ["known"], "warnings": "bad"}, "ungültige Warnungen"),
    ],
)
def test_assemble_payload_rejects_invalid_selection(selection: dict, match: str) -> None:
    concepts = {"known": {"path": "known.md", "content": "x"}}
    with pytest.raises(RuntimeError, match=match):
        _assemble_knowledge_payload(selection, concepts)
