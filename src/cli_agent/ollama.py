from __future__ import annotations

from typing import Any

import httpx

from .model import CONTEXT_LIMIT_MARGIN, ContextLimitReachedError, TokenUsage

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
        context_length: int | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.context_length = context_length
        self.last_usage: TokenUsage | None = None
        self.usage_history: list[TokenUsage | None] = []
        self._context_limit_reached = False

    def _context_limit_error(self) -> ContextLimitReachedError:
        usage = self.last_usage
        if usage is None or self.context_length is None:
            return ContextLimitReachedError(
                "Context-Limit der aktuellen Sitzung wurde erreicht. "
                "Es werden keine weiteren Modellanfragen gesendet. Bitte starte "
                "eine neue Sitzung."
            )
        return ContextLimitReachedError(
            "Context-Limit der aktuellen Sitzung wurde erreicht. "
            f"Input des letzten Modellaufrufs: {usage.input_tokens} Tokens; "
            f"konfiguriertes Limit: {self.context_length} Tokens; "
            f"Sicherheitsreserve: {CONTEXT_LIMIT_MARGIN} Tokens. "
            "Die Modellantwort wurde verworfen. Es werden keine weiteren "
            "Modellanfragen gesendet. Bitte starte eine neue Sitzung."
        )

    def _check_context_limit(self) -> None:
        if self._context_limit_reached:
            raise self._context_limit_error()
        if (
            self.context_length is not None
            and self.last_usage is not None
            and self.last_usage.input_tokens + CONTEXT_LIMIT_MARGIN
            >= self.context_length
        ):
            self._context_limit_reached = True
            raise self._context_limit_error()

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        self._check_context_limit()

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
        self._check_context_limit()

        message = data.get("message")
        if not isinstance(message, dict):
            raise OllamaError(f"Unerwartete Ollama-Antwort: {data}")
        return message
