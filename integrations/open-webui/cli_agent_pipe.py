"""
title: cli-agent
author: cli-agent
version: 0.1.0
description: Local cli-agent API integration with interactive tool approvals.
requirements: httpx>=0.27,<1
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
from pydantic import BaseModel, Field


class Pipe:
    class Valves(BaseModel):
        API_URL: str = Field(default="http://127.0.0.1:8765")
        API_TOKEN: str = Field(default="", json_schema_extra={"input": {"type": "password"}})
        TIMEOUT_SECONDS: float = Field(default=600, ge=10, le=3600)

    def __init__(self):
        self.type = "pipe"
        self.id = "cli_agent"
        self.name = "cli-agent"
        self.valves = self.Valves()
        self._sessions: dict[tuple[str, str], str] = {}
        self._lock = asyncio.Lock()

    def pipes(self):
        return [{"id": "agent", "name": "Agent"}]

    async def _status(self, emitter, description: str, done: bool = False):
        if emitter is not None:
            await emitter({"type": "status", "data": {
                "description": description, "done": done,
            }})

    async def pipe(
        self, body: dict, __user__: dict | None = None,
        __event_emitter__=None, __event_call__=None,
        __metadata__: dict | None = None,
    ):
        if not self.valves.API_TOKEN:
            raise ValueError("API_TOKEN muss in den Pipe-Valves konfiguriert werden.")
        # Never accept a remote URL: the API token and workspace access belong
        # exclusively to the local server-side integration.
        from urllib.parse import urlsplit
        url = urlsplit(self.valves.API_URL)
        if (url.scheme != "http" or url.hostname != "127.0.0.1"
                or url.username or url.password or url.query or url.fragment
                or url.port is None or url.path not in ("", "/")):
            raise ValueError("API_URL muss http://127.0.0.1:<port> sein.")

        messages = body.get("messages") or []
        if not isinstance(messages, list):
            raise ValueError("Ungültiger Nachrichtenverlauf.")
        prompt = next(
            (m.get("content") for m in reversed(messages)
             if isinstance(m, dict) and m.get("role") == "user"
             and isinstance(m.get("content"), str) and m["content"].strip()),
            None,
        )
        if prompt is None:
            raise ValueError("Keine Textnachricht des Benutzers vorhanden.")

        user_id = str((__user__ or {}).get("id") or "")
        chat_id = str((__metadata__ or {}).get("chat_id") or body.get("chat_id") or "")
        # No stable chat identity => ephemeral session, not shared across users.
        persistent = bool(user_id and chat_id)
        key = (user_id, chat_id)
        headers = {"Authorization": f"Bearer {self.valves.API_TOKEN}"}
        timeout = httpx.Timeout(self.valves.TIMEOUT_SECONDS, connect=15)
        base = self.valves.API_URL.rstrip("/")

        async with httpx.AsyncClient(
            base_url=base, headers=headers, timeout=timeout, trust_env=False
        ) as client:
            async with self._lock:
                sid = self._sessions.get(key) if persistent else None
                if sid is None:
                    response = await client.post("/sessions")
                    response.raise_for_status()
                    sid = response.json()["session_id"]
                    if persistent:
                        self._sessions[key] = sid

            try:
                response = await client.post(
                    f"/sessions/{sid}/runs", json={"prompt": prompt}
                )
                if response.status_code in (404, 409) and persistent:
                    # A stale session (API restart) can be replaced, but never
                    # retry a busy run (409) and risk duplicate execution.
                    if response.status_code == 404:
                        async with self._lock:
                            if self._sessions.get(key) == sid:
                                self._sessions.pop(key, None)
                        response = await client.post("/sessions")
                        response.raise_for_status()
                        sid = response.json()["session_id"]
                        async with self._lock:
                            self._sessions[key] = sid
                        response = await client.post(
                            f"/sessions/{sid}/runs", json={"prompt": prompt}
                        )
                response.raise_for_status()
                rid = response.json()["run_id"]
                await self._status(__event_emitter__, "cli-agent arbeitet …")
                answer = ""
                async with client.stream(
                    "GET", f"/sessions/{sid}/runs/{rid}/events",
                    headers={"Accept": "text/event-stream"},
                ) as stream:
                    stream.raise_for_status()
                    async for line in stream.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        event = json.loads(line[6:])
                        kind = event.get("type")
                        if kind == "TEXT_MESSAGE_CONTENT":
                            answer += event.get("delta", "")
                        elif kind == "TOOL_CALL_START":
                            await self._status(
                                __event_emitter__,
                                f"Tool: {event.get('toolCallName', 'unbekannt')}",
                            )
                        elif kind == "CUSTOM":
                            name = event.get("name")
                            value = event.get("value") or {}
                            if name == "approval_required":
                                decision = "no"
                                if (__event_call__ is not None
                                        and not value.get("diff_blocked")):
                                    details = json.dumps(
                                        {k: v for k, v in value.items() if k != "id"},
                                        ensure_ascii=False, indent=2,
                                    )
                                    result = await __event_call__({
                                        "type": "confirmation",
                                        "data": {
                                            "title": "cli-agent Tool-Freigabe",
                                            "message": details[:12000],
                                        },
                                    })
                                    if result is True or (
                                        isinstance(result, dict)
                                        and result.get("confirmed") is True
                                    ):
                                        decision = "yes"
                                approval_id = value.get("id")
                                if not isinstance(approval_id, str):
                                    raise ValueError("Ungültige Approval-ID.")
                                approval = await client.post(
                                    f"/sessions/{sid}/runs/{rid}/approvals/{approval_id}",
                                    json={"decision": decision},
                                )
                                approval.raise_for_status()
                            elif name == "mcp_user_message":
                                await self._status(
                                    __event_emitter__,
                                    str(value.get("content", ""))[:250],
                                )
                        elif kind == "RUN_ERROR":
                            raise RuntimeError("Agentenlauf fehlgeschlagen.")
                status = await client.get(f"/sessions/{sid}/runs/{rid}")
                status.raise_for_status()
                snapshot = status.json()
                if snapshot.get("status") != "completed":
                    raise RuntimeError("Agentenlauf nicht erfolgreich beendet.")
                await self._status(__event_emitter__, "Fertig", done=True)
                return answer or snapshot.get("answer") or ""
            finally:
                if not persistent:
                    await client.delete(f"/sessions/{sid}")
