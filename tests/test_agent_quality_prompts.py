"""Deterministic prompt-contract checks; live LLM behaviour needs separate evals."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cli_agent.agent import BASE_SYSTEM_PROMPT, MCP_TOOL_SYSTEM_RULE, CliAgent
from cli_agent.data_mcp_server import _server_instructions
from cli_agent.ollama import OllamaClient


FIXTURE = Path(__file__).parent / "fixtures" / "agent_quality_timeseries.json"


def _agent(tmp_path: Path) -> CliAgent:
    return CliAgent(
        tmp_path,
        OllamaClient(base_url="http://localhost:11434", model="test"),
        (),
    )


def test_evidence_rules_apply_without_mcp_tools(tmp_path: Path) -> None:
    prompt = _agent(tmp_path)._build_system_prompt()
    assert "Superlative durch vollständige Vergleiche" in prompt
    assert "sonst benenne Kandidaten" in prompt
    assert "Messwert, Berechnung und Hypothese" in prompt
    assert "Ereignisse an\nkonkreten Daten" in prompt
    assert "UTC-/Lokalzeiten mit explizitem Offset" in prompt
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
