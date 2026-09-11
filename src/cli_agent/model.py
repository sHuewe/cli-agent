from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


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
