from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from typing import Any, Protocol


CONTEXT_LIMIT_MARGIN = 500
logger = logging.getLogger("cli_agent.model")


class ContextLimitReachedError(RuntimeError):
    """Configured context limit was reached for the current model session."""




class ModelRequestError(RuntimeError):
    """A model request failed before a usable model message was returned."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class ModelRetryPolicy:
    max_attempts: int = 1
    initial_delay_seconds: float = 1.0
    backoff_multiplier: float = 2.0
    max_delay_seconds: float = 10.0

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_attempts, bool)
            or not isinstance(self.max_attempts, int)
            or not 1 <= self.max_attempts <= 10
        ):
            raise ValueError(
                "max_attempts muss eine Ganzzahl zwischen 1 und 10 sein."
            )
        for name, value, lower, upper in (
            (
                "initial_delay_seconds",
                self.initial_delay_seconds,
                0,
                60,
            ),
            (
                "backoff_multiplier",
                self.backoff_multiplier,
                1,
                10,
            ),
            (
                "max_delay_seconds",
                self.max_delay_seconds,
                0,
                300,
            ),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not lower <= value <= upper
            ):
                raise ValueError(
                    f"{name} muss eine Zahl zwischen {lower} und {upper} sein."
                )


class RetryingModelClient:
    """Retry only transient model requests, never complete agent/tool steps."""

    def __init__(
        self,
        client: ModelClient,
        policy: ModelRetryPolicy,
    ) -> None:
        self._client = client
        self._policy = policy
        self.model = client.model
        self.base_url = client.base_url
        self.context_length = getattr(client, "context_length", None)

    @property
    def last_finish_reason(self) -> str | None:
        return getattr(self._client, "last_finish_reason", None)

    @property
    def last_usage(self) -> TokenUsage | None:
        return self._client.last_usage

    @property
    def usage_history(self) -> list[TokenUsage | None]:
        return self._client.usage_history

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        think: bool | None = None,
    ) -> dict[str, Any]:
        delay = min(
            self._policy.initial_delay_seconds,
            self._policy.max_delay_seconds,
        )
        for attempt in range(1, self._policy.max_attempts + 1):
            try:
                if think is None:
                    return await self._client.chat(messages, tools)
                return await self._client.chat(
                    messages,
                    tools,
                    think=think,
                )
            except ModelRequestError as exc:
                if not exc.retryable or attempt >= self._policy.max_attempts:
                    raise
                logger.warning(
                    "model_request_retry attempt=%d max_attempts=%d "
                    "error_type=%s delay_seconds=%s",
                    attempt + 1,
                    self._policy.max_attempts,
                    type(exc).__name__,
                    delay,
                )
                if delay > 0:
                    await asyncio.sleep(delay)
                delay = min(
                    delay * self._policy.backoff_multiplier,
                    self._policy.max_delay_seconds,
                )
        raise AssertionError("unreachable")


@dataclass(frozen=True)
class TokenUsage:
    """Normalized token usage for one model request."""

    input_tokens: int
    output_tokens: int
    total_tokens: int


def log_model_call_diagnostics(client: Any, *, phase: str) -> None:
    """Log reported provider usage; missing values stay explicitly unknown."""
    usage = getattr(client, "last_usage", None)
    context_length = getattr(client, "context_length", None)
    if not isinstance(context_length, int) or context_length <= 0:
        context_length = None
    input_tokens = getattr(usage, "input_tokens", None)
    output_tokens = getattr(usage, "output_tokens", None)
    total_tokens = getattr(usage, "total_tokens", None)
    utilization = (
        round(100 * input_tokens / context_length, 2)
        if isinstance(input_tokens, int) and context_length else None
    )
    history = getattr(client, "usage_history", None)
    logger.info(
        "model_call phase=%s model=%s call_index=%s input_tokens=%s "
        "output_tokens=%s total_tokens=%s context_length=%s "
        "context_utilization_percent=%s finish_reason=%s",
        phase, getattr(client, "model", "unknown"),
        len(history) if isinstance(history, list) else "unknown",
        input_tokens if input_tokens is not None else "unknown",
        output_tokens if output_tokens is not None else "unknown",
        total_tokens if total_tokens is not None else "unknown",
        context_length if context_length is not None else "unknown",
        utilization if utilization is not None else "unknown",
        getattr(client, "last_finish_reason", None) or "unknown",
    )


class ModelClient(Protocol):
    model: str
    base_url: str
    last_usage: TokenUsage | None
    usage_history: list[TokenUsage | None]

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        think: bool | None = None,
    ) -> dict[str, Any]:
        ...
