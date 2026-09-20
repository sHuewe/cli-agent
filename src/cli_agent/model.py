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
        if isinstance(self.max_attempts, bool) or not 1 <= self.max_attempts <= 10:
            raise ValueError("max_attempts muss zwischen 1 und 10 liegen.")
        if not 0 <= self.initial_delay_seconds <= 60:
            raise ValueError(
                "initial_delay_seconds muss zwischen 0 und 60 liegen."
            )
        if not 1 <= self.backoff_multiplier <= 10:
            raise ValueError(
                "backoff_multiplier muss zwischen 1 und 10 liegen."
            )
        if not 0 <= self.max_delay_seconds <= 300:
            raise ValueError(
                "max_delay_seconds muss zwischen 0 und 300 liegen."
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
        delay = self._policy.initial_delay_seconds
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
