from __future__ import annotations

from typing import Any, Protocol


class ModelClient(Protocol):
    model: str
    base_url: str

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        think: bool | None = None,
    ) -> dict[str, Any]:
        ...