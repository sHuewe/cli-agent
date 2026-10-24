"""Single-tab leases for sessions of the embedded browser UI.

The browser-wide access token identifies the locally launched UI.  A separate
opaque lease identifies the *current page instance* owning one agent session.
Tickets are single-use capabilities for opening a session in a new tab.
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Any


LEASE_TTL_SECONDS = 25.0
TICKET_TTL_SECONDS = 60.0


class SessionOccupiedError(RuntimeError):
    """The selected agent session is already owned by an active browser tab."""


@dataclass
class _Lease:
    token: str
    last_seen: float


class BrowserSessionRegistry:
    def __init__(self, service: Any, initial_session_id: str) -> None:
        self.service = service
        self._managed = {initial_session_id}
        self._leases: dict[str, _Lease] = {}
        self._tickets: dict[str, tuple[str, float]] = {}

    def _valid(self, session_id: str) -> bool:
        return session_id in self._managed and session_id in self.service.sessions

    def _occupied(self, session_id: str) -> bool:
        lease = self._leases.get(session_id)
        if lease is None:
            return False
        if time.monotonic() - lease.last_seen >= LEASE_TTL_SECONDS:
            del self._leases[session_id]
            return False
        return True

    def register(self, session_id: str) -> None:
        if session_id not in self.service.sessions:
            raise KeyError("Session nicht gefunden.")
        self._managed.add(session_id)

    def list_sessions(self) -> list[dict[str, Any]]:
        # Only UI-created sessions are listed; externally created API sessions
        # must never become visible or controllable through a browser token.
        return [
            {
                "session_id": sid,
                "occupied": self._occupied(sid),
                "busy": bool(self.service.sessions[sid]._busy),
            }
            for sid in sorted(self._managed)
            if sid in self.service.sessions
        ]

    def invitation(self, session_id: str) -> str:
        if not self._valid(session_id):
            raise KeyError("Session nicht gefunden.")
        if self._occupied(session_id):
            raise SessionOccupiedError("Session ist bereits in einem anderen Tab geöffnet.")
        ticket = secrets.token_urlsafe(32)
        self._tickets[ticket] = (session_id, time.monotonic() + TICKET_TTL_SECONDS)
        return ticket

    def _acquire(self, session_id: str) -> str:
        if not self._valid(session_id):
            raise KeyError("Session nicht gefunden.")
        if self._occupied(session_id):
            raise SessionOccupiedError("Session ist bereits in einem anderen Tab geöffnet.")
        token = secrets.token_urlsafe(32)
        self._leases[session_id] = _Lease(token, time.monotonic())
        return token

    def claim(self, session_id: str) -> str:
        # Used for the initial page and for reloading the same tab after the
        # previous page has released its lease. Duplicate tabs are rejected.
        return self._acquire(session_id)

    def redeem(self, ticket: str) -> tuple[str, str]:
        # Tickets are consumed atomically, even on a failed/occupied claim.
        record = self._tickets.pop(ticket, None)
        if record is None or time.monotonic() > record[1]:
            raise ValueError("Einmaliger Session-Link ist ungültig oder abgelaufen.")
        session_id = record[0]
        return session_id, self._acquire(session_id)

    def is_owner(self, session_id: str, token: str | None, *, refresh: bool = True) -> bool:
        if not token or not self._valid(session_id) or not self._occupied(session_id):
            return False
        lease = self._leases[session_id]
        if not secrets.compare_digest(token, lease.token):
            return False
        if refresh:
            lease.last_seen = time.monotonic()
        return True

    def release(self, session_id: str, token: str | None) -> bool:
        if not self.is_owner(session_id, token, refresh=False):
            return False
        del self._leases[session_id]
        return True

    def can_close(self, session_id: str) -> bool:
        if not self._valid(session_id):
            raise KeyError("Session nicht gefunden.")
        if self._occupied(session_id) or self.service.sessions[session_id]._busy:
            return False
        return True

    def forget(self, session_id: str) -> None:
        self._managed.discard(session_id)
        self._leases.pop(session_id, None)
        for ticket, (sid, _) in tuple(self._tickets.items()):
            if sid == session_id:
                self._tickets.pop(ticket, None)
