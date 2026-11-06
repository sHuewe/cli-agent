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
            # Both the original and the restored live session referenced the
            # deleted snapshot. Neither may try to overwrite it on next save.
            assert original.saved_snapshot_id is None
            assert service.sessions[restored_id].saved_snapshot_id is None
            again = await client.post("/ui/saved-sessions", headers=own,
                                      json={"session_id": original.id,
                                            "name": "Saved again"})
            assert again.status_code == 201, again.text
            assert again.json()["id"] != snapshot_id
            ticket = restored.json()["ticket"]
            restored_boot = await client.post("/ui/bootstrap", headers=headers,
                                              json={"ticket": ticket})
            assert restored_boot.status_code == 200
            restored_lease = restored_boot.json()["lease"]
            saved_restored = await client.post(
                "/ui/saved-sessions",
                headers={**headers, "X-Cli-Agent-Lease": restored_lease},
                json={"session_id": restored_id, "name": "Restored and saved"},
            )
            assert saved_restored.status_code == 201, saved_restored.text
            assert saved_restored.json()["id"] not in {snapshot_id, again.json()["id"]}
    finally:
        await service.shutdown()


def test_restored_context_preview_uses_current_policy_and_saved_history():
    from cli_agent.runtime.agent_conversation import ConversationMixin

    class Agent(ConversationMixin):
        history = [
            {"role": "user", "content": "Earlier question"},
            {"role": "assistant", "content": "Earlier answer"},
        ]
        _evidence_state = EvidenceState()

        def _build_system_prompt(self):
            return "Current configured policy"

    agent = Agent()
    before = list(agent.history)
    preview = agent.restored_working_messages_snapshot()
    assert preview[0] == {"role": "system", "content": "Current configured policy"}
    assert preview[1:] == before
    preview[1]["content"] = "Modified preview"
    assert agent.history == before


@pytest.mark.asyncio
async def test_saved_session_working_context_shows_reconstructed_preview(tmp_path):
    class ContextAgent(FakeAgent):
        def __init__(self):
            super().__init__()
            # Simulate working messages from agent initial commands: they
            # must not leak into a restored session's diagnostic view.
            self._last_working_messages = [
                {"role": "user", "content": "Startup initialization"},
            ]
            self._last_working_tools = [{"type": "function"}]

        def working_messages_snapshot(self):
            if self._last_working_messages:
                return list(self._last_working_messages)
            # Evidence can generate a non-empty projection even when no
            # model loop has run yet. This must not mask restored history.
            return [{"role": "user", "content": "Evidence-only projection"}]

        def restored_working_messages_snapshot(self):
            return [
                {"role": "system", "content": "Current policy"},
                *self.history,
            ]

        def tool_states_snapshot(self):
            return []

        def context_states_snapshot(self):
            return []

        def okf_status_snapshot(self):
            return {"configured": False}

    storage = SessionPersistence(tmp_path)
    service = AgentApiService(lambda session: ContextAgent(), session_storage=storage)
    try:
        original = await service.create()
        original.agent.history = [
            {"role": "user", "content": "Saved question"},
            {"role": "assistant", "content": "Saved answer"},
        ]
        original.ui_messages[:] = list(original.agent.history)
        saved = storage.save(original, "Saved investigation")
        restored = await service.restore_saved_session(saved["id"])
        assert restored._restored_from_snapshot is True
        assert restored.agent._last_working_messages == []
        assert restored.agent._last_working_tools == []
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(service)),
            base_url="http://127.0.0.1",
        ) as client:
            url = f"/sessions/{restored.id}/working-context"
            resp = await client.get(
                url, headers={"Authorization": "Bearer " + service.token},
            )
            assert resp.status_code == 200, resp.text
            payload = resp.json()
            assert payload["restored_preview"] is True
            assert payload["messages"] == [
                {"role": "system", "content": "Current policy"},
                *original.agent.history,
            ]
            assert payload["tools"] == []
            # Once a real main loop has populated working messages, the
            # original diagnostics take precedence over the preview.
            restored.agent._last_working_messages = [
                {"role": "user", "content": "Actual model context"},
            ]
            resp = await client.get(
                url, headers={"Authorization": "Bearer " + service.token},
            )
            assert resp.status_code == 200
            assert resp.json()["restored_preview"] is False
            assert resp.json()["messages"] == restored.agent._last_working_messages
    finally:
        await service.shutdown()
