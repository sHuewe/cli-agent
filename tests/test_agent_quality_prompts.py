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
    assert "bearbeitet genau EINE Analyseetappe innerhalb EINES" in prompt
    assert "Trenne Kandidatensuche, Detailprüfung und unabhängige Validierung" in prompt
    assert "Ein einzelner Planschritt darf mehrere Sub-Runs benötigen" in prompt
    assert "genau EIN überprüfbares Zwischenergebnis" in prompt
    assert "unterteile ihn in kleinere, einzeln überprüfbare" in prompt
    assert "mehrerer Planschritte" in prompt
    assert "klare Grenzen" in prompt
    assert "Hauptagent bleibt Koordinator und Prüfer" in prompt
    assert "begründeten Ausnahmefällen" in prompt
    assert "früh" in prompt.lower()

    description = SUB_RUN_TOOL["function"]["description"]
    assert "ONE coordinator plan step" in description
    assert "smaller part of that step" in description
    assert "Split large plan steps" in description
    assert "candidate discovery, focused validation, and synthesis" in description
    assert "ONE verifiable intermediate result" in description
    assert "additional model" in description
    step = SUB_RUN_TOOL["function"]["parameters"]["properties"]["plan_step_id"]
    assert "several smaller delegated work packages" in step["description"]



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



def test_sub_run_prompt_includes_coordinator_session_timezone(monkeypatch, tmp_path: Path) -> None:
    import asyncio
    from cli_agent.sub_runs import execute_sub_run
    import cli_agent.agent_loop as agent_loop

    seen = []

    async def fake_run_model_loop(agent, *, messages, **kwargs):
        seen.append(messages[0]["content"])
        return json.dumps({
            "status": "partial", "summary": "No finding yet.",
            "evidence_ids": [], "confidence": "low", "uncertainties": [],
        })

    monkeypatch.setattr(agent_loop, "run_model_loop", fake_run_model_loop)
    agent = _agent(tmp_path, agent_timezone="Europe/Berlin")
    agent._sub_runs = []
    asyncio.run(execute_sub_run(agent, {
        "task": "Check a local-day electricity series using the session timezone.",
    }))
    assert len(seen) == 1
    assert agent.agent_timezone == "Europe/Berlin"
    assert agent._format_current_time(datetime.now(timezone.utc))[:20] in seen[0]
    assert "(Europe/Berlin)" in seen[0]
    assert "Aktuelle lokale Zeit:" in seen[0]


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
            "status": "partial", "summary": "No verifiable findings yet.",
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
        _format_current_time=lambda now: "Aktuelle lokale Zeit: " + now.isoformat(),
    )
    result = asyncio.run(execute_sub_run(agent, {
        "task": "Investigate a sufficiently complex and independent work package.",
    }))
    assert MAX_SUB_RUN_TOOLS == 200
    from cli_agent.sub_runs import MAX_SUB_RUNS_PER_TASK
    assert MAX_SUB_RUNS_PER_TASK == 10
    assert seen == [(200, "sub")]
    assert result["status"] == "partial"
    assert agent._evidence_state is not None
    assert not hasattr(agent, "_coordinator_evidence_state")



def test_delegated_evidence_context_resolves_only_trusted_live_artifacts() -> None:
    from cli_agent.evidence_state import EvidenceState
    from cli_agent.sub_runs import _delegated_evidence_context

    state = EvidenceState()
    state.register_result("data-call", tool_name="data__resample_data",
                          artifact_metadata={"workspace_path": ".cli-agent/real.json",
                                             "records_path": "/rows", "row_count": 8})
    state.register_result("plain-call", tool_name="sensor__get_values")
    state.add_evidence({"id": "selected", "type": "observation",
                        "statement": "Eight rows processed.", "source_refs": ["S1", "S2"]})
    state.add_evidence({"id": "other", "type": "observation",
                        "statement": "Other observation.", "source_refs": ["S2"]})
    context = _delegated_evidence_context(state, ["selected"])
    assert [e["id"] for e in context["existing_evidences"]] == ["selected"]
    assert context["source_artifacts"] == {
        "S1": {"workspace_path": ".cli-agent/real.json", "records_path": "/rows"}
    }
    assert state.evidences["selected"]["tool_calls"][0]["workspace_path"] == ".cli-agent/real.json"

    state.begin_task()
    assert _delegated_evidence_context(state, ["selected"])["source_artifacts"] == {}


def test_sub_run_prompt_explains_existing_evidence_artifacts_and_handoff(tmp_path: Path) -> None:
    from cli_agent.sub_runs import SUB_RUN_RULES, SUB_RUN_TOOL

    agent = _agent(tmp_path)
    agent._active_servers = {"example"}
    agent._server_tools = {"example": [{"function": {"name": "example__search"}}]}
    prompt = agent._build_system_prompt()
    assert "in getrennten Modellschritten" in prompt
    assert "evidence_ids" in prompt
    assert "bloße Erwähnung im task reicht" in prompt
    assert "source_artifacts" in SUB_RUN_RULES
    assert "workspace_path" in SUB_RUN_RULES
    assert "source_artifacts" in SUB_RUN_TOOL["function"]["parameters"]["properties"]["evidence_ids"]["description"]



def test_sub_run_retries_invalid_response_using_same_context_and_budget(monkeypatch) -> None:
    import asyncio
    import json
    from types import SimpleNamespace

    from cli_agent.evidence_state import EvidenceState
    from cli_agent.sub_runs import execute_sub_run
    import cli_agent.agent_loop as agent_loop

    seen = []

    async def fake_run_model_loop(agent, *, messages, run_state, phase, **kwargs):
        assert phase == "sub"
        seen.append((id(messages), id(run_state), list(messages)))
        return (
            "This is not JSON."
            if len(seen) == 1
            else json.dumps({
                "status": "partial", "summary": "Investigation has no verified finding.",
                "evidence_ids": [], "confidence": "medium",
                "uncertainties": [],
            })
        )

    monkeypatch.setattr(agent_loop, "run_model_loop", fake_run_model_loop)
    agent = SimpleNamespace(
        max_tool_calls=15, _sub_runs=[], _evidence_state=EvidenceState(),
        _active_servers=set(), _server_instructions={}, _tool_routes={},
        _model_tools=lambda: [],
        _build_reference_context_message=lambda **kwargs: None,
        _main_tools_need_workspace_context=lambda names: False,
        _format_current_time=lambda now: "Aktuelle lokale Zeit: " + now.isoformat(),
    )
    result = asyncio.run(execute_sub_run(agent, {
        "task": "Investigate the selected time intervals using trusted data.",
    }))
    assert result["status"] == "partial"
    assert len(seen) == 2
    assert seen[0][0:2] == seen[1][0:2]
    assert "Validierungsfehler" in seen[1][2][-1]["content"]
    assert agent._sub_runs[0]["status"] == "partial"


def test_sub_run_stops_after_bounded_invalid_json_retries(monkeypatch) -> None:
    import asyncio
    from types import SimpleNamespace
    import pytest

    from cli_agent.evidence_state import EvidenceState
    from cli_agent.sub_runs import execute_sub_run
    from cli_agent.agent_conversation import MAX_RESPONSE_FORMAT_REPAIRS
    import cli_agent.agent_loop as agent_loop

    invocations = []

    async def invalid_response(agent, *, messages, run_state, **kwargs):
        invocations.append((id(messages), id(run_state)))
        return "Still not JSON."

    monkeypatch.setattr(agent_loop, "run_model_loop", invalid_response)
    agent = SimpleNamespace(
        _sub_runs=[], _evidence_state=EvidenceState(),
        _active_servers=set(), _server_instructions={}, _tool_routes={},
        _model_tools=lambda: [],
        _build_reference_context_message=lambda **kwargs: None,
        _main_tools_need_workspace_context=lambda names: False,
        _format_current_time=lambda now: "Aktuelle lokale Zeit: " + now.isoformat(),
    )
    with pytest.raises(ValueError, match="nach 2 Korrekturversuchen"):
        asyncio.run(execute_sub_run(agent, {
            "task": "Investigate the selected intervals and return findings.",
        }))
    assert len(invocations) == MAX_RESPONSE_FORMAT_REPAIRS + 1
    assert len(set(invocations)) == 1
    assert agent._sub_runs[0]["status"] == "failed"


def test_sub_run_context_limit_does_not_poison_coordinator(monkeypatch, tmp_path: Path) -> None:
    import asyncio
    import pytest
    from cli_agent.model import ContextLimitReachedError
    from cli_agent.sub_runs import execute_sub_run
    import cli_agent.agent_loop as agent_loop

    class Client:
        def __init__(self):
            self._context_limit_reached = False
            self.last_usage = "coordinator-usage"
            self.last_finish_reason = "stop"
            self.usage_history = []

    coordinator = _agent(tmp_path)
    coordinator.model_client = Client()
    coordinator._sub_runs = []
    original = coordinator.model_client
    children = []

    async def fake_run_model_loop(agent, **kwargs):
        child = agent.model_client
        children.append(child)
        assert child is not original
        assert child._context_limit_reached is False
        assert child.last_usage is None
        child._context_limit_reached = True
        child.last_usage = "child-usage"
        child.usage_history.append("child-usage")
        raise ContextLimitReachedError("Child context full")

    monkeypatch.setattr(agent_loop, "run_model_loop", fake_run_model_loop)
    with pytest.raises(ContextLimitReachedError, match="Child context full"):
        asyncio.run(execute_sub_run(coordinator, {
            "task": "Investigate enough events to exceed the delegated context window.",
        }))
    assert coordinator.model_client is original
    assert original._context_limit_reached is False
    assert original.last_usage == "coordinator-usage"
    assert original.last_finish_reason == "stop"
    assert original.usage_history == ["child-usage"]
    assert coordinator._sub_runs[0]["status"] == "failed"


def test_completed_sub_run_requires_new_source_backed_evidence() -> None:
    import pytest
    from cli_agent.sub_runs import SUB_RUN_RULES, validate_sub_result
    assert "agent__add_evidence" in SUB_RUN_RULES
    assert "MUST create at least one new evidence" in SUB_RUN_RULES
    completed = {
        "status": "completed", "summary": "Found a result",
        "evidence_ids": [], "confidence": "high", "uncertainties": [],
    }
    with pytest.raises(ValueError, match="mindestens eine NEUE Evidence"):
        validate_sub_result(json.dumps(completed), set())
    assert validate_sub_result(
        json.dumps({**completed, "evidence_ids": ["new-finding"]}),
        {"new-finding"},
    )["evidence_ids"] == ["new-finding"]
    assert validate_sub_result(
        json.dumps({**completed, "status": "partial"}),
        set(),
    )["evidence_ids"] == []


def test_sub_run_evidence_tools_ignore_plan_ids_and_hide_schema_field() -> None:
    from cli_agent.evidence_state import EvidenceState
    from cli_agent.sub_runs import _delegated_evidence_context, SUB_RUN_RULES

    parent = EvidenceState()
    parent.register_result("call-one", tool_name="sensor")
    parent.update_plan({"goal": "Investigate", "steps": [
        {"id": "step-1", "description": "Analyze", "status": "in_progress"}
    ]})
    parent.add_evidence({"id": "existing", "type": "observation",
                         "statement": "Original finding.", "source_refs": ["S1"],
                         "plan_step_id": "step-1"})
    inherited = _delegated_evidence_context(parent, ["existing"])
    assert "plan_step_id" not in inherited["existing_evidences"][0]

    child = EvidenceState()
    child._sub_run_no_plan = True
    child.register_result("child-call", tool_name="sensor")
    child.evidences["existing"] = dict(parent.evidences["existing"])
    child.evidences["existing"].pop("plan_step_id")
    tools = child.tool_definitions()
    for tool in tools[1:]:
        assert "plan_step_id" not in tool["function"]["parameters"]["properties"]
    child.add_evidence({"id": "new", "type": "observation",
                        "statement": "Child finding.", "source_refs": ["S1"],
                        "plan_step_id": "step-1"})
    assert "plan_step_id" not in child.evidences["new"]
    child.update_evidence({"id": "new", "statement": "Updated finding.",
                           "plan_step_id": "step-1"})
    assert "plan_step_id" not in child.evidences["new"]
    child.register_result("child-call-two", tool_name="sensor")
    assert all("plan_step_id" not in tool["function"]["parameters"]["properties"]
               for tool in child.tool_definitions()[1:])
    assert "Sub-runs have NO local plan" in SUB_RUN_RULES


def test_delegation_warns_about_fanout_and_soft_budget(tmp_path: Path) -> None:
    from cli_agent.sub_runs import SUB_RUN_TOOL, SUB_RUN_SOFT_TOOL_LIMIT

    agent = _agent(tmp_path)
    agent._active_servers = {"example"}
    agent._server_tools = {"example": [{"function": {"name": "example__search"}}]}
    prompt = agent._build_system_prompt()
    assert "Anzahl Elemente mal Schritte" in prompt
    assert "20 bis 50 Toolaufrufe" in prompt
    assert "keine offenen Aufträge" in prompt
    assert "Estimate fan-out" in SUB_RUN_TOOL["function"]["description"]
    assert SUB_RUN_SOFT_TOOL_LIMIT == 120


def test_sub_run_soft_limit_returns_partial_without_executing_large_batch() -> None:
    import asyncio
    from types import SimpleNamespace
    from cli_agent.agent_loop import ModelLoopRunState, run_model_loop
    from cli_agent.evidence_state import EvidenceState
    from cli_agent.config import LoggingConfig

    class Model:
        async def chat(self, *, messages, tools):
            return {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call-" + str(i), "type": "function",
                 "function": {"name": "external__read", "arguments": {}}}
                for i in range(4)
            ]}

    evidence = EvidenceState()
    evidence._sub_run_inherited_ids = {"old"}
    evidence.register_result("source-call", tool_name="external__read")
    evidence.add_evidence({"id": "new-finding", "type": "observation",
                           "statement": "Verified fact", "source_refs": ["S1"]})
    agent = SimpleNamespace(
        _evidence_state=evidence, model_client=Model(),
        logging_config=LoggingConfig(),
        _dump_context=lambda *a, **k: None,
    )
    state = ModelLoopRunState(tool_calls=118)
    output = asyncio.run(run_model_loop(
        agent, messages=[{"role": "system", "content": "test"}],
        tools=[], routes={}, enabled_server_names=set(),
        max_tool_calls=200, phase="sub", run_state=state,
    ))
    result = json.loads(output)
    assert result["status"] == "partial"
    assert result["evidence_ids"] == ["new-finding"]
    assert state.tool_calls == 118


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
