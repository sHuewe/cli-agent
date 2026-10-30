"""Deterministic prompt-contract checks; live LLM behaviour needs separate evals."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cli_agent.agent import BASE_SYSTEM_PROMPT, MCP_TOOL_SYSTEM_RULE, CliAgent
from cli_agent.data_mcp_server import _server_instructions
from cli_agent.ollama import OllamaClient


FIXTURE = Path(__file__).parent / "fixtures" / "agent_quality_timeseries.json"


def _agent(tmp_path: Path, *, agent_timezone: str | None = None) -> CliAgent:
    return CliAgent(
        tmp_path,
        OllamaClient(base_url="http://localhost:11434", model="test"),
        (),
        agent_timezone=agent_timezone,
    )


def test_system_prompt_uses_system_local_time_by_default(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    instant = datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc)
    assert agent._format_current_time(instant) == (
        f"Aktuelle lokale Zeit: {instant.astimezone().isoformat()}"
    )
    assert "Aktuelle lokale Zeit:" in agent._build_system_prompt()


def test_configured_timezone_distinguishes_dst_instants(tmp_path: Path) -> None:
    agent = _agent(tmp_path, agent_timezone="Europe/Berlin")
    summer = datetime(2026, 7, 1, 12, tzinfo=timezone.utc)
    winter = datetime(2026, 12, 1, 12, tzinfo=timezone.utc)
    assert "+02:00 (Europe/Berlin)" in agent._format_current_time(summer)
    assert "+01:00 (Europe/Berlin)" in agent._format_current_time(winter)
    first = datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc)
    second = first + timedelta(hours=1)
    assert agent._format_current_time(first) == (
        "Aktuelle lokale Zeit: 2026-10-25T02:30:00+02:00 (Europe/Berlin)"
    )
    assert agent._format_current_time(second) == (
        "Aktuelle lokale Zeit: 2026-10-25T02:30:00+01:00 (Europe/Berlin)"
    )


def test_evidence_rules_apply_without_mcp_tools(tmp_path: Path) -> None:
    prompt = _agent(tmp_path)._build_system_prompt()
    assert "Superlative durch vollständige Vergleiche" in prompt
    assert "sonst benenne Kandidaten" in prompt
    assert "belegte Fakten, Ableitungen und Hypothesen" in prompt
    assert "verlässlicher Quellen" in prompt
    assert "Zeitzonen und Zeitgrenzen" in prompt
    assert "grundloser Datei-Inventarisierung" not in prompt


def test_tool_reuse_rule_only_applies_when_tools_exist(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    agent._active_servers = {"example"}
    agent._server_tools = {
        "example": [{"function": {"name": "example__history"}}]
    }
    prompt = agent._build_system_prompt()
    assert "Rufe Tools nur bei Erkenntnisgewinn auf" in prompt
    assert "nutze vorhandene Daten" in prompt
    assert "redundanter Abfragen oder grundloser Datei-Inventarisierung" in prompt
    assert prompt.count("Superlative durch vollständige Vergleiche") == 1


def test_data_instructions_reuse_artifacts_and_known_pointers() -> None:
    instructions = _server_instructions("read")
    assert "workspace_path" in instructions
    assert "Always propagate both" in instructions
    assert "Reuse artifacts even for narrower ranges" in instructions
    assert "refetch only if insufficient" in instructions
    assert "Skip inspect_json when records_path is known" in instructions



def test_sub_run_guidance_encourages_early_single_task_delegation(tmp_path: Path) -> None:
    from cli_agent.sub_runs import START_SUB_RUN, SUB_RUN_TOOL

    agent = _agent(tmp_path)
    agent._active_servers = {"example"}
    agent._server_tools = {
        "example": [{"function": {"name": "example__search"}}]
    }
    assert START_SUB_RUN in [
        tool["function"]["name"] for tool in agent._model_tools()
    ]
    prompt = agent._build_system_prompt()
    assert "EINE abgegrenzte Teiluntersuchung" in prompt
    assert "viele Toolaufrufe" in prompt
    assert "starte den Sub-Run vor ihrer" in prompt
    assert "einzelne Toolabfragen" in prompt
    assert "Hauptagent ist Koordinator und Prüfer" in prompt
    assert "ein klar definiertes Arbeitspaket" in prompt
    assert "vollständige operative Delegation ist möglich" in prompt

    description = SUB_RUN_TOOL["function"]["description"]
    assert "even one" in description
    assert "coordinator context" in description
    assert "additional model" in description



def test_sub_run_evidence_ids_contract_distinguishes_saved_findings_from_sources() -> None:
    from cli_agent.sub_runs import SUB_RUN_TOOL

    function = SUB_RUN_TOOL["function"]
    field = function["parameters"]["properties"]["evidence_ids"]
    assert "agent__add_evidence" in field["description"]
    assert "source_ref" in field["description"]
    assert "omit" in field["description"]
    assert "S1/S2" in function["description"]


def test_sub_run_invalid_source_refs_receive_actionable_feedback() -> None:
    import asyncio
    from types import SimpleNamespace
    import pytest
    from cli_agent.evidence_state import EvidenceState
    from cli_agent.sub_runs import execute_sub_run

    coordinator = SimpleNamespace(_sub_runs=[], _evidence_state=EvidenceState())
    with pytest.raises(ValueError, match="source_refs, keine Evidence-IDs") as error:
        asyncio.run(execute_sub_run(coordinator, {
            "task": "Investigate the selected time intervals and compare results.",
            "evidence_ids": ["S1", "S4"],
        }))
    assert "agent__add_evidence" in str(error.value)
    assert "lasse evidence_ids weg" in str(error.value)
    assert coordinator._sub_runs == []



def test_sub_run_has_200_tool_calls_independent_of_coordinator_limit(monkeypatch) -> None:
    import asyncio
    import json
    from types import SimpleNamespace
    from cli_agent.evidence_state import EvidenceState
    from cli_agent.sub_runs import MAX_SUB_RUN_TOOLS, execute_sub_run
    import cli_agent.agent_loop as agent_loop

    seen = []

    async def fake_run_model_loop(agent, *, max_tool_calls, phase, **kwargs):
        seen.append((max_tool_calls, phase))
        return json.dumps({
            "status": "completed", "summary": "No evidence was needed.",
            "evidence_ids": [], "confidence": "low", "uncertainties": [],
        })

    monkeypatch.setattr(agent_loop, "run_model_loop", fake_run_model_loop)
    agent = SimpleNamespace(
        max_tool_calls=15,
        _sub_runs=[],
        _evidence_state=EvidenceState(),
        _active_servers=set(),
        _server_instructions={},
        _tool_routes={},
        _model_tools=lambda: [],
        _build_reference_context_message=lambda **kwargs: None,
        _main_tools_need_workspace_context=lambda names: False,
    )
    result = asyncio.run(execute_sub_run(agent, {
        "task": "Investigate a sufficiently complex and independent work package.",
    }))
    assert MAX_SUB_RUN_TOOLS == 200
    from cli_agent.sub_runs import MAX_SUB_RUNS_PER_TASK
    assert MAX_SUB_RUNS_PER_TASK == 10
    assert seen == [(200, "sub")]
    assert result["status"] == "completed"
    assert agent._evidence_state is not None
    assert not hasattr(agent, "_coordinator_evidence_state")


def test_prompt_changes_remain_compact() -> None:
    assert len(BASE_SYSTEM_PROMPT) + len(MCP_TOOL_SYSTEM_RULE) <= 850
    assert len(_server_instructions("read")) <= 1_743


def test_eval_fixture_has_two_comparable_events_and_distinct_dst_instants() -> None:
    rows = json.loads(FIXTURE.read_text(encoding="utf-8"))["rows"]
    assert rows[1]["temperature"] - rows[0]["temperature"] == -2.0
    assert rows[3]["temperature"] - rows[2]["temperature"] == -2.5

    first = datetime.fromisoformat(rows[4]["timestamp"].replace("Z", "+00:00"))
    second = datetime.fromisoformat(rows[5]["timestamp"].replace("Z", "+00:00"))
    summer = timezone(timedelta(hours=2))
    winter = timezone(timedelta(hours=1))
    assert first.astimezone(summer).strftime("%H:%M%z") == "02:30+0200"
    assert second.astimezone(winter).strftime("%H:%M%z") == "02:30+0100"
    assert second - first == timedelta(hours=1)
