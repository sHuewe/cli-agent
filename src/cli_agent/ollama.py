from __future__ import annotations

from typing import Any

import httpx

from .model import TokenUsage

ctx_large = 24576
ctx_small = 8192


class OllamaError(RuntimeError):
    """Ollama could not provide a usable response."""


class OllamaClient:
    @staticmethod
    def _convert_messages(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        converted = []

        for message in messages:
            result = dict(message)
            result.pop("tool_call_id", None)
            converted.append(result)

        return converted

    @staticmethod
    def _token_usage(data: dict[str, Any]) -> TokenUsage | None:
        input_tokens = data.get("prompt_eval_count")
        output_tokens = data.get("eval_count")
        if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
            return None
        return TokenUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
        )

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        timeout: float = 120.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.last_usage: TokenUsage | None = None
        self.usage_history: list[TokenUsage | None] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "messages": self._convert_messages(messages),
            "tools": tools,
            "stream": False,
            "options": {
                "num_ctx": ctx_large
            },
        }
        self.last_usage = None
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(f"{self.base_url}/api/chat", json=payload)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise OllamaError(
                f"Ollama unter {self.base_url} ist nicht erreichbar: {exc}"
            ) from exc

        data = response.json()
        self.last_usage = self._token_usage(data)
        self.usage_history.append(self.last_usage)

        message = data.get("message")
        if not isinstance(message, dict):
            raise OllamaError(f"Unerwartete Ollama-Antwort: {data}")
        return message
