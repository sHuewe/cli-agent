"""Saved session round trips, limits, isolation and browser authorization."""
from __future__ import annotations

import json
import uuid

import httpx
import pytest

from cli_agent.interfaces.agent_api import AgentApiService, create_app
from cli_agent.interfaces.session_persistence import SessionPersistence
from cli_agent.interfaces.web_ui_sessions import BrowserSessionRegistry
from cli_agent.state.evidence_state import EvidenceState


class FakeAgent:
    def __init__(self):
        self.history = []
        self._evidence_state = EvidenceState()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def ask(self, prompt):
        self.history.extend([{"role": "user", "content": prompt},
                             {"role": "assistant", "content": "answer"}])
        return "answer"


def test_save_load_delete_durable_state(tmp_path):
    storage = SessionPersistence(tmp_path)
    class FakeSession:
        _busy = False
        _closed = False
        _pending_interrupt = None
        agent = FakeAgent()
        ui_messages = [{"role": "user", "content": "Q"},
                       {"role": "assistant", "content": "A"}]
        agui_messages = []
    session = FakeSession()
    session.agent.history = list(session.ui_messages)
    state = session.agent._evidence_state
    state.tool_results["call-1"] = {
        "source_ref": "S1", "tool_name": "data__aggregate",
        "workspace_path": ".cli-agent/mcp-data-123/result.csv",
    }
    state._next_source_number = 2
    state.evidences["e1"] = {
        "id": "e1", "type": "observation", "statement": "Value is 3",
        "tool_calls": [{"call_id": "call-1", "source_ref": "S1",
                        "tool_name": "data__aggregate", "description": "Aggregated values",
                        "workspace_path": ".cli-agent/mcp-data-123/result.csv"}],
        "plan_step_id": "step-1",
    }
    saved = storage.save(session, "Energy analysis")
    assert saved["name"] == "Energy analysis"
    assert len(storage.list()) == 1
    restored = storage.load(saved["id"])
    assert restored["history"] == session.agent.history
    assert restored["ui_messages"] == session.ui_messages
    assert restored["evidence"].evidences["e1"]["statement"] == "Value is 3"
    assert "plan_step_id" not in restored["evidence"].evidences["e1"]
    assert "workspace_path" not in restored["evidence"].tool_results["call-1"]
    assert "workspace_path" not in restored["evidence"].evidences["e1"]["tool_calls"][0]
    assert restored["evidence"]._next_source_number == 2

    saved2 = storage.save(session, "Renamed analysis", snapshot_id=saved["id"])
    assert saved2["id"] == saved["id"]
    assert storage.list()[0]["name"] == "Renamed analysis"
    storage.delete(saved["id"])
    assert storage.list() == []


def test_snapshot_rejects_unsafe_path_and_invalid_messages(tmp_path):
    storage = SessionPersistence(tmp_path)
    with pytest.raises(ValueError):
        storage.load("../escape")
    with pytest.raises(ValueError):
        storage.delete("x" * 32)
    class Session:
        _busy = False
        _closed = False
        _pending_interrupt = None
        agent = FakeAgent()
        ui_messages = []
        agui_messages = []
    session = Session()
    session.agent.history = [{"role": "tool", "content": "raw tool output"}]
    with pytest.raises(ValueError):
        storage.save(session, "Bad messages")
    session.agent.history = []
    session._busy = True
    with pytest.raises(RuntimeError):
        storage.save(session, "Busy")
    session._busy = False
    snap = storage.save(session, "Safe")
    folder = tmp_path / ".cli-agent" / "sessions" / snap["id"]
    (folder / "session.json").unlink()
    (folder / "session.json").symlink_to(tmp_path / "external.json")
    with pytest.raises(ValueError):
        storage.load(snap["id"])


@pytest.mark.asyncio
async def test_browser_save_and_restore_requires_owning_lease(tmp_path):
    storage = SessionPersistence(tmp_path)
    service = AgentApiService(lambda _session: FakeAgent(), session_storage=storage)
    original = await service.create()
    registry = BrowserSessionRegistry(service, original.id)
    origin = "http://127.0.0.1:9191"
    ui = {
        "token": "browser-secret", "origin": origin,
        "session_id": original.id, "registry": registry,
        "info": {"model": "test", "workspace": ".", "mcp_servers": []},
        "shutdown": lambda: None,
    }
    headers = {"Authorization": "Bearer browser-secret",
               "X-Cli-Agent-Browser": "1", "Origin": origin}
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(service, ui=ui)),
                                    base_url=origin) as client:
            boot = await client.post("/ui/bootstrap", headers=headers, json={})
            lease = boot.json()["lease"]
            save = {"session_id": original.id, "name": "My project"}
            assert (await client.post("/ui/saved-sessions", headers=headers,
                                      json=save)).status_code == 403
            own = {**headers, "X-Cli-Agent-Lease": lease}
            response = await client.post("/ui/saved-sessions", headers=own, json=save)
            assert response.status_code == 201, response.text
            snapshot_id = response.json()["id"]
            restored = await client.post(f"/ui/saved-sessions/{snapshot_id}/load",
                                         headers=headers, json={})
            assert restored.status_code == 201, restored.text
            restored_id = restored.json()["session_id"]
            assert restored_id != original.id
            assert restored_id in service.sessions
            assert storage.list()[0]["id"] == snapshot_id
            assert (await client.delete(f"/ui/saved-sessions/{snapshot_id}",
                                        headers=headers)).status_code == 204
    finally:
        await service.shutdown()
