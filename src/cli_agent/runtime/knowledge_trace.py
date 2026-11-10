"""Opt-in, bounded Knowledge observation for the Python SDK.

The collector is deliberately separate from the Knowledge search/selection
algorithm. It only snapshots host-verified retrievals and final context;
recording failures cannot influence the agent's answer or permissions.
Nothing is exported through normal runtime events or CLI/API responses.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import copy
import json
import logging
import math
from time import monotonic
from typing import Any

_LOGGER = logging.getLogger(__name__)
MAX_TRACE_SEARCHES = 128
MAX_TRACE_HITS = 256
MAX_TRACE_SELECTED = 80
MAX_TRACE_CONTENT_CHARS = 512_000
MAX_TRACE_METADATA_CHARS = 4096


@dataclass(frozen=True)
class KnowledgeHit:
    """One verified search, document read, or final selected context item."""

    id: str | None
    source: str | None
    rank: int
    content: str | None = None
    score: float | None = None
    distance: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class KnowledgeSearch:
    """One retrieval operation; ranking is local to this operation."""

    backend: str
    stage: str  # initial | followup | read
    query: str | None
    hits: tuple[KnowledgeHit, ...]


@dataclass(frozen=True)
class KnowledgeCall:
    """One initial or additional Knowledge-phase invocation."""

    origin: str  # initial | reentry
    status: str  # selected | not_found | not_applicable | no_content | error
    duration_ms: float


@dataclass(frozen=True)
class KnowledgeTrace:
    """One SDK question's bounded Knowledge retrieval and selection trace."""

    searches: tuple[KnowledgeSearch, ...] = ()
    selected: tuple[KnowledgeHit, ...] = ()
    calls: tuple[KnowledgeCall, ...] = ()
    truncated: bool = False
    content_included: bool = False

    @property
    def complete_selected_content(self) -> bool:
        return bool(self.selected) and not self.truncated and all(
            isinstance(hit.content, str) and bool(hit.content)
            for hit in self.selected
        )


class KnowledgeTraceCollector:
    """Per-ask mutable collector, never shared between requests or sessions."""

    def __init__(self, *, include_content: bool = False) -> None:
        self.include_content = include_content
        self.searches: list[KnowledgeSearch] = []
        self.selected: list[KnowledgeHit] = []
        self.calls: list[KnowledgeCall] = []
        self.truncated = False
        self._text_used = 0
        self._hit_count = 0
        self._call_started: float | None = None
        self._call_origin: str | None = None
        self._explicit_status: str | None = None

    def begin(self, origin: str) -> None:
        if self._call_started is not None:
            # Reentry starts only after the initial Knowledge call completes.
            raise RuntimeError("Nested Knowledge trace call")
        self._call_started = monotonic()
        self._call_origin = origin
        self._explicit_status = None

    def outcome(self, status: str) -> None:
        """Preserve results that runtime normalizes to None for optional knowledge."""
        if status not in {"selected", "not_found", "not_applicable", "no_content", "error"}:
            raise ValueError("Invalid Knowledge outcome")
        self._explicit_status = status

    def mark_truncated(self) -> None:
        """Indicate that a backend's context budget excluded raw search hits."""
        self.truncated = True

    def finish(self, result: str | None = None, *, error: bool = False) -> None:
        started = self._call_started
        if started is None:
            return
        status = "error" if error else "no_content"
        if result is not None and not error:
            status = "selected"
            try:
                payload = json.loads(result)
            except (TypeError, ValueError):
                payload = None
            if isinstance(payload, dict) and payload.get("found_content") is False:
                reason = payload.get("reason_code")
                status = reason if reason in {"not_found", "not_applicable"} else "no_content"
        if self._explicit_status is not None:
            status = self._explicit_status
        if error:
            status = "error"
        self.calls.append(KnowledgeCall(
            origin=self._call_origin or "initial", status=status,
            duration_ms=round((monotonic() - started) * 1000, 3),
        ))
        self._call_started = None
        self._call_origin = None
        self._explicit_status = None

    def _string(self, value: Any, *, limit: int = 2048) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            self.truncated = True
            return None
        if len(value) > limit:
            self.truncated = True
            return None
        return value

    def _content(self, value: Any) -> str | None:
        if not self.include_content or value is None:
            return None
        if not isinstance(value, str) or not value:
            self.truncated = True
            return None
        if self._text_used + len(value) > MAX_TRACE_CONTENT_CHARS:
            self.truncated = True
            return None
        self._text_used += len(value)
        return value

    def _metadata(self, metadata: Any) -> dict[str, Any]:
        if metadata is None:
            return {}
        if not isinstance(metadata, dict):
            self.truncated = True
            return {}
        try:
            copied = copy.deepcopy(metadata)
            if len(json.dumps(copied, ensure_ascii=False)) > MAX_TRACE_METADATA_CHARS:
                self.truncated = True
                return {}
        except (TypeError, ValueError, RecursionError):
            self.truncated = True
            return {}
        return copied

    @staticmethod
    def _number(value: Any) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value) if math.isfinite(value) else None

    def _hit(self, item: dict[str, Any], *, rank: int) -> KnowledgeHit:
        return KnowledgeHit(
            id=self._string(item.get("id") or item.get("path") or item.get("concept"), limit=2048),
            source=self._string(item.get("source") or item.get("path") or item.get("concept"), limit=2048),
            rank=rank,
            content=self._content(item.get("content")),
            score=self._number(item.get("score")),
            distance=self._number(item.get("distance")),
            metadata=self._metadata(item.get("metadata")),
        )

    def search(self, *, backend: str, stage: str, result: dict[str, Any]) -> None:
        if len(self.searches) >= MAX_TRACE_SEARCHES:
            self.truncated = True
            return
        entries = result.get("entries", [])
        if not isinstance(entries, list):
            self.truncated = True
            return
        hits: list[KnowledgeHit] = []
        for rank, item in enumerate(entries, start=1):
            if self._hit_count >= MAX_TRACE_HITS:
                self.truncated = True
                break
            if not isinstance(item, dict):
                self.truncated = True
                continue
            hits.append(self._hit(item, rank=rank))
            self._hit_count += 1
        query = self._string(result.get("query"), limit=2000)
        self.searches.append(KnowledgeSearch(
            backend=backend, stage=stage, query=query, hits=tuple(hits),
        ))

    def read(self, document: dict[str, Any]) -> None:
        path = document.get("path")
        self.search(
            backend="okf", stage="read",
            result={"entries": [{
                "id": path, "source": path, "content": document.get("content"),
            }]},
        )

    def selection(self, *, backend: str, payload: dict[str, Any]) -> None:
        entries = payload.get("content", [])
        if not isinstance(entries, list):
            self.truncated = True
            return
        for item in entries:
            if len(self.selected) >= MAX_TRACE_SELECTED:
                self.truncated = True
                break
            if not isinstance(item, dict):
                self.truncated = True
                continue
            # OKF payload uses "concept"; Chroma uses "id"/"source".
            self.selected.append(self._hit(item, rank=len(self.selected) + 1))

    def snapshot(self) -> KnowledgeTrace:
        # The underlying data is owned by the returned immutable snapshot;
        # this collector is discarded at the end of the SDK question.
        return KnowledgeTrace(
            searches=tuple(self.searches), selected=tuple(self.selected),
            calls=tuple(self.calls), truncated=self.truncated,
            content_included=self.include_content,
        )


def trace_observe(agent: Any, action: str, **data: Any) -> None:
    """Never let optional instrumentation alter Knowledge search or answers."""
    collector = getattr(agent, "_sdk_knowledge_trace_collector", None)
    if collector is None:
        return
    try:
        getattr(collector, action)(**data)
    except Exception:
        # Even a malformed collector must not affect the normal agent run.
        # Do not log source data, query text, or raw exception messages.
        try:
            collector.truncated = True
        except Exception:
            pass
        _LOGGER.warning("sdk_knowledge_trace_record_failed action=%s", action)
