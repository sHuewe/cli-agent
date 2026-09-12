from __future__ import annotations

import json
from typing import Any

import httpx

from .model import CONTEXT_LIMIT_MARGIN, ContextLimitReachedError, TokenUsage


class OpenAIError(RuntimeError):
    pass


class OpenAIClient:
    @staticmethod
    def _convert_messages(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        converted: list[dict[str, Any]] = []

        for message in messages:
            result = dict(message)

            result.pop("thinking", None)
            result.pop("tool_name", None)

            tool_calls = result.get("tool_calls")
            if tool_calls:
                converted_calls = []

                for tool_call in tool_calls:
                    converted_call = dict(tool_call)
                    function = dict(
                        converted_call.get("function", {})
                    )

                    arguments = function.get("arguments")
                    if isinstance(arguments, dict):
                        function["arguments"] = json.dumps(
                            arguments,
                            ensure_ascii=False,
                        )

                    converted_call["function"] = function
                    converted_calls.append(converted_call)

                result["tool_calls"] = converted_calls

            converted.append(result)

        return converted

    @staticmethod
    def _normalize_message(
        message: dict[str, Any],
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "role": "assistant",
            "content": message.get("content") or "",
        }

        tool_calls = message.get("tool_calls")
        if tool_calls:
            normalized_calls = []

            for tool_call in tool_calls:
                normalized_call = dict(tool_call)
                function = dict(
                    normalized_call.get("function", {})
                )

                arguments = function.get("arguments", {})
                if isinstance(arguments, str):
                    try:
                        function["arguments"] = json.loads(arguments)
                    except json.JSONDecodeError:
                        function["arguments"] = arguments

                normalized_call["function"] = function
                normalized_calls.append(normalized_call)

            result["tool_calls"] = normalized_calls

        return result

    @staticmethod
    def _token_usage(data: dict[str, Any]) -> TokenUsage | None:
        usage = data.get("usage")
        if not isinstance(usage, dict):
            return None

        input_tokens = usage.get("prompt_tokens", usage.get("input_tokens"))
        output_tokens = usage.get(
            "completion_tokens",
            usage.get("output_tokens"),
        )
        if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
            return None

        total_tokens = usage.get("total_tokens")
        if not isinstance(total_tokens, int):
            total_tokens = input_tokens + output_tokens

        return TokenUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
        )

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None,
        timeout: float = 120.0,
        headers: dict[str, str] | None = None,
        context_length: int | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.headers = dict(headers or {})
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
        *,
        think: bool | None = None,
    ) -> dict[str, Any]:
        self._check_context_limit()

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._convert_messages(messages),
            "stream": False,
        }

        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        headers = {
            "Content-Type": "application/json",
            **self.headers,
        }

        if self.api_key:
            headers.setdefault(
                "Authorization",
                f"Bearer {self.api_key}",
            )

        self.last_usage = None
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout
            ) as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers=headers,
                    json=payload,
                )
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise OpenAIError(
                f"OpenAI-kompatibles Modell unter "
                f"{self.base_url} nicht erreichbar: {exc}"
            ) from exc

        data = response.json()
        self.last_usage = self._token_usage(data)
        self.usage_history.append(self.last_usage)
        self._check_context_limit()

        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise OpenAIError(
                f"Unerwartete Modellantwort: {data}"
            ) from exc

        return self._normalize_message(message)
