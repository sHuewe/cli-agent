from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli_agent.admin_config import WebProviderConfig
from cli_agent.model import TokenUsage
from cli_agent.web_context import (
    WebContext,
    _confluence_api_url,
    _confluence_document,
    _confluence_page_reference,
    _extract_web_content,
    _validate_web_url,
    redact_url_for_display,
)
from cli_agent.web_context_agent import LoopTokenUsage, WebContextCliAgent


def test_extract_html_uses_main_content() -> None:
    title, content = _extract_web_content(
        """
        <html>
          <head>
            <title>API Docs</title>
            <style>.hidden { display: none; }</style>
            <script>do_not_include()</script>
          </head>
          <body>
            <nav>Home Products Pricing Contact</nav>
            <main>
              <article>
                <h1>Users API</h1>
                <p>GET /users returns the available users.</p>
              </article>
            </main>
            <footer>Legal Privacy Imprint</footer>
          </body>
        </html>
        """,
        "text/html",
        url="https://example.org/docs",
    )

    assert title == "API Docs"
    assert "Users API" in content
    assert "GET /users" in content
    assert "do_not_include" not in content
    assert "display: none" not in content


def test_extract_plain_text_is_unchanged_except_normalization() -> None:
    title, content = _extract_web_content(
        "First line\r\n\r\nSecond   line",
        "text/plain",
    )

    assert title is None
    assert content == "First line\n\nSecond line"


def test_validate_web_url_requires_explicit_hosts() -> None:
    with pytest.raises(ValueError, match="nicht erlaubt"):
        _validate_web_url("http://localhost:8080/docs")
    with pytest.raises(ValueError, match="nicht erlaubt"):
        _validate_web_url("http://10.1.2.3/docs")
    assert (
        _validate_web_url(
            "http://localhost:8080/docs",
            allowed_hosts=("localhost",),
        )
        == "http://localhost:8080/docs"
    )


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/test.html",
        "ftp://example.org/file",
        "https://user:secret@example.org/docs",
    ],
)
def test_validate_web_url_rejects_unsupported_urls(url: str) -> None:
    with pytest.raises(ValueError):
        _validate_web_url(url)


class RecordingModel:
    model = "test"
    base_url = "test://model"

    def __init__(self) -> None:
        self.calls = []

    async def chat(self, messages, tools, **kwargs):
        self.calls.append((messages.copy(), tools.copy()))
        return {"role": "assistant", "content": "ok"}


class UsageRecordingModel(RecordingModel):
    def __init__(self, usages: list[TokenUsage | None]) -> None:
        super().__init__()
        self.pending_usages = list(usages)
        self.usage_history: list[TokenUsage | None] = []
        self.last_usage: TokenUsage | None = None

    async def chat(self, messages, tools, **kwargs):
        self.calls.append((messages.copy(), tools.copy()))
        self.last_usage = self.pending_usages.pop(0)
        self.usage_history.append(self.last_usage)
        return {"role": "assistant", "content": "ok"}


def make_agent(
    tmp_path: Path,
    model: RecordingModel | None = None,
) -> WebContextCliAgent:
    agent = WebContextCliAgent(
        tmp_path,
        model or RecordingModel(),
        (),
    )
    agent._exit_stack = SimpleNamespace()
    return agent


def example_context(url: str = "http://localhost:8080/docs") -> WebContext:
    return WebContext(
        requested_url=url,
        final_url=url,
        title="Local API",
        content="Users API\nGET /users",
        fetched_at="2026-09-08T15:00:00+00:00",
    )


def test_redact_url_for_display_removes_query_and_fragment() -> None:
    assert (
        redact_url_for_display(
            "https://example.org/docs?access_token=secret#session-secret"
        )
        == "https://example.org/docs"
    )


def test_web_context_model_payload_redacts_query_and_fragment() -> None:
    context = WebContext(
        requested_url=(
            "https://example.org/docs?access_token=request-secret"
            "#request-fragment"
        ),
        final_url=(
            "https://example.org/final?signature=final-secret"
            "#final-fragment"
        ),
        title="Docs",
        content="Reference content",
        fetched_at="2026-09-19T18:00:00+00:00",
    )

    payload = context.as_dict()
    serialized = str(payload)

    assert payload["requested_url"] == "https://example.org/docs"
    assert payload["final_url"] == "https://example.org/final"
    assert "request-secret" not in serialized
    assert "request-fragment" not in serialized
    assert "final-secret" not in serialized
    assert "final-fragment" not in serialized


def test_add_web_context_keeps_full_url_internal_but_redacts_cli_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested = (
        "http://localhost:8080/docs?access_token=request-secret"
        "#request-fragment"
    )
    seen: list[str] = []

    async def fake_fetch(url: str, **_: object) -> WebContext:
        seen.append(url)
        return WebContext(
            requested_url=url,
            final_url=(
                "http://localhost:8080/final?signature=final-secret"
                "#final-fragment"
            ),
            title="Local API",
            content="Users API",
            fetched_at="2026-09-19T18:00:00+00:00",
        )

    monkeypatch.setattr("cli_agent.web_context_agent.fetch_web_context", fake_fetch)
    agent = make_agent(tmp_path)

    answer = asyncio.run(agent.ask(f"add_web_context {requested}"))

    assert seen == [requested]
    assert agent._web_contexts[0].requested_url == requested
    assert "http://localhost:8080/final" in answer
    assert "request-secret" not in answer
    assert "request-fragment" not in answer
    assert "final-secret" not in answer
    assert "final-fragment" not in answer


def test_model_reference_context_does_not_disclose_url_secrets(
    tmp_path: Path,
) -> None:
    model = RecordingModel()
    agent = make_agent(tmp_path, model)
    agent._web_contexts.append(
        WebContext(
            requested_url=(
                "http://localhost:8080/docs?access_token=request-secret"
                "#request-fragment"
            ),
            final_url=(
                "http://localhost:8080/final?signature=final-secret"
                "#final-fragment"
            ),
            title="Local API",
            content="Users API\nGET /users",
            fetched_at="2026-09-19T18:00:00+00:00",
        )
    )

    assert asyncio.run(agent.ask("Welche API ist beschrieben?")) == "ok"

    reference_content = model.calls[-1][0][-2]["content"]
    assert "http://localhost:8080/docs" in reference_content
    assert "http://localhost:8080/final" in reference_content
    assert "request-secret" not in reference_content
    assert "request-fragment" not in reference_content
    assert "final-secret" not in reference_content
    assert "final-fragment" not in reference_content


def test_add_web_context_is_session_command_not_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_fetch(url: str, **_: object) -> WebContext:
        return example_context(url)

    monkeypatch.setattr("cli_agent.web_context_agent.fetch_web_context", fake_fetch)
    agent = make_agent(tmp_path)

    answer = asyncio.run(agent.ask("add_web_context http://localhost:8080/docs"))

    assert "Web-Kontext hinzugefügt" in answer
    assert len(agent._web_contexts) == 1
    assert agent.history == []


def test_web_context_is_separate_working_message_but_not_history(
    tmp_path: Path,
) -> None:
    model = RecordingModel()
    agent = make_agent(tmp_path, model)
    agent._web_contexts.append(example_context())

    answer = asyncio.run(agent.ask("Welche API ist beschrieben?"))

    assert answer == "ok"
    messages = model.calls[-1][0]
    assert messages[-1] == {"role": "user", "content": "Welche API ist beschrieben?"}
    reference_message = messages[-2]
    assert reference_message["role"] == "user"
    assert "http://localhost:8080/docs" in reference_message["content"]
    assert "GET /users" in reference_message["content"]
    assert "Welche API ist beschrieben?" not in reference_message["content"]
    assert agent.history == [
        {"role": "user", "content": "Welche API ist beschrieben?"},
        {"role": "assistant", "content": "ok"},
    ]
    assert "GET /users" not in str(agent.history)


def test_clear_web_context_removes_all_contexts_without_history(tmp_path: Path) -> None:
    agent = make_agent(tmp_path)
    agent._web_contexts.extend(
        [
            example_context(),
            example_context("http://10.1.2.3/docs"),
        ]
    )

    answer = asyncio.run(agent.ask("clear_web_context"))

    assert answer == "Web-Kontext gelöscht (2 Einträge)."
    assert agent._web_contexts == []
    assert agent.history == []


def test_loop_token_usage_aggregates_requests() -> None:
    usage = LoopTokenUsage.from_requests(
        [
            TokenUsage(input_tokens=100, output_tokens=10, total_tokens=110),
            TokenUsage(input_tokens=250, output_tokens=20, total_tokens=270),
        ]
    )

    assert usage == LoopTokenUsage(
        requests=2,
        usage_requests=2,
        input_tokens=350,
        output_tokens=30,
        total_tokens=380,
        max_input_tokens=250,
        last_input_tokens=250,
    )


def test_loop_token_usage_preserves_missing_requests() -> None:
    usage = LoopTokenUsage.from_requests(
        [
            TokenUsage(input_tokens=100, output_tokens=10, total_tokens=110),
            TokenUsage(input_tokens=250, output_tokens=20, total_tokens=270),
            None,
        ]
    )

    assert usage == LoopTokenUsage(
        requests=3,
        usage_requests=2,
        input_tokens=350,
        output_tokens=30,
        total_tokens=380,
        max_input_tokens=250,
        last_input_tokens=None,
    )


def test_tokens_command_reports_main_and_knowledge_usage(tmp_path: Path) -> None:
    model = UsageRecordingModel(
        [
            TokenUsage(input_tokens=1_500, output_tokens=100, total_tokens=1_600),
            TokenUsage(input_tokens=3_000, output_tokens=200, total_tokens=3_200),
        ]
    )
    agent = make_agent(tmp_path, model)

    asyncio.run(
        agent._run_model_loop(
            messages=[{"role": "system", "content": "Knowledge"}],
            tools=[],
            routes={},
            enabled_server_names=None,
            max_tool_calls=1,
            phase="knowledge",
        )
    )
    asyncio.run(
        agent._run_model_loop(
            messages=[{"role": "system", "content": "Main"}],
            tools=[],
            routes={},
            enabled_server_names=set(),
            max_tool_calls=1,
            phase="main",
        )
    )

    calls_before = len(model.calls)
    answer = asyncio.run(agent.ask("tokens"))

    assert "Main-Loop:" in answer
    assert "Usage verfügbar: 1/1" in answer
    assert "Input gesamt: 3.000 Tokens" in answer
    assert "Output gesamt: 200 Tokens" in answer
    assert "Max. gemeldeter Input eines Aufrufs: 3.000 Tokens" in answer
    assert "Knowledge-Loop:" in answer
    assert "Input gesamt: 1.500 Tokens" in answer
    assert len(model.calls) == calls_before


def test_tokens_command_marks_mixed_usage_as_incomplete(tmp_path: Path) -> None:
    usage = LoopTokenUsage.from_requests(
        [
            TokenUsage(input_tokens=1_000, output_tokens=100, total_tokens=1_100),
            TokenUsage(input_tokens=2_000, output_tokens=200, total_tokens=2_200),
            None,
        ]
    )
    assert usage is not None

    text = WebContextCliAgent._format_loop_usage(
        "Main-Loop",
        ran=True,
        usage=usage,
    )

    assert "Modellaufrufe: 3" in text
    assert "Usage verfügbar: 2/3" in text
    assert "Input gesamt: mindestens 3.000 Tokens" in text
    assert "Input letzter Aufruf: nicht verfügbar" in text
    assert "Usage-Daten sind unvollständig" in text


def test_tokens_command_reports_all_missing_usage(tmp_path: Path) -> None:
    usage = LoopTokenUsage.from_requests([None, None])
    assert usage is not None

    text = WebContextCliAgent._format_loop_usage(
        "Main-Loop",
        ran=True,
        usage=usage,
    )

    assert "Modellaufrufe: 2" in text
    assert "Usage verfügbar: 0/2" in text
    assert "keine Usage-Daten geliefert" in text


def test_tokens_command_does_not_clear_previous_usage(tmp_path: Path) -> None:
    model = UsageRecordingModel(
        [TokenUsage(input_tokens=500, output_tokens=50, total_tokens=550)]
    )
    agent = make_agent(tmp_path, model)

    assert asyncio.run(agent.ask("Hallo")) == "ok"
    first = asyncio.run(agent.ask("tokens"))
    second = asyncio.run(agent.ask("tokens"))

    assert first == second
    assert "Main-Loop:" in first
    assert "Knowledge-Loop: nicht ausgeführt." in first


def test_add_web_context_typo_does_not_call_model(tmp_path: Path) -> None:
    model = RecordingModel()
    agent = make_agent(tmp_path, model)

    answer = asyncio.run(
        agent.ask("add_web_contex http://localhost:8080/docs")
    )

    assert "Meintest du: add_web_context <URL>?" in answer
    assert model.calls == []
    assert agent.history == []


def test_add_web_context_missing_url_does_not_call_model(tmp_path: Path) -> None:
    model = RecordingModel()
    agent = make_agent(tmp_path, model)

    answer = asyncio.run(agent.ask("add_web_context"))

    assert "Verwendung: add_web_context <URL>" in answer
    assert model.calls == []
    assert agent.history == []


def test_normal_similar_text_still_reaches_model(tmp_path: Path) -> None:
    model = RecordingModel()
    agent = make_agent(tmp_path, model)

    answer = asyncio.run(agent.ask("Erkläre mir add web context"))

    assert answer == "ok"
    assert len(model.calls) == 1


def test_uppercase_enable_is_normalized_before_delegation(tmp_path: Path) -> None:
    model = RecordingModel()
    agent = make_agent(tmp_path, model)
    called: list[tuple[str, bool]] = []

    async def fake_set_server_enabled(server_name: str, *, enabled: bool) -> bool:
        called.append((server_name, enabled))
        return True

    agent.set_server_enabled = fake_set_server_enabled  # type: ignore[method-assign]

    answer = asyncio.run(agent.ask("ENABLE docs"))

    assert answer == "MCP-Server docs aktiviert."
    assert called == [("docs", True)]
    assert model.calls == []


def test_uppercase_disable_is_normalized_before_delegation(tmp_path: Path) -> None:
    model = RecordingModel()
    agent = make_agent(tmp_path, model)
    called: list[tuple[str, bool]] = []

    async def fake_set_server_enabled(server_name: str, *, enabled: bool) -> bool:
        called.append((server_name, enabled))
        return True

    agent.set_server_enabled = fake_set_server_enabled  # type: ignore[method-assign]

    answer = asyncio.run(agent.ask("DISABLE docs"))

    assert answer == "MCP-Server docs deaktiviert."
    assert called == [("docs", False)]
    assert model.calls == []


def test_add_web_contexts_prose_reaches_model(tmp_path: Path) -> None:
    model = RecordingModel()
    agent = make_agent(tmp_path, model)

    answer = asyncio.run(agent.ask("add_web_contexts usage in Python"))

    assert answer == "ok"
    assert len(model.calls) == 1


def test_add_web_context_call_syntax_prose_reaches_model(tmp_path: Path) -> None:
    model = RecordingModel()
    agent = make_agent(tmp_path, model)

    answer = asyncio.run(agent.ask("add_web_context() usage"))

    assert answer == "ok"
    assert len(model.calls) == 1


def test_tokens_prose_reaches_model(tmp_path: Path) -> None:
    model = RecordingModel()
    agent = make_agent(tmp_path, model)

    answer = asyncio.run(agent.ask("tokens in this prompt"))

    assert answer == "ok"
    assert len(model.calls) == 1


def test_enable_prose_reaches_model(tmp_path: Path) -> None:
    model = RecordingModel()
    agent = make_agent(tmp_path, model)

    answer = asyncio.run(agent.ask("enable dark mode in the UI"))

    assert answer == "ok"
    assert len(model.calls) == 1


def test_add_web_context_non_url_prose_reaches_model(tmp_path: Path) -> None:
    model = RecordingModel()
    agent = make_agent(tmp_path, model)

    answer = asyncio.run(agent.ask("add_web_context usage"))

    assert answer == "ok"
    assert len(model.calls) == 1


def _confluence_provider() -> WebProviderConfig:
    return WebProviderConfig(
        provider_type="confluence",
        base_url="https://wiki.example.org/confluence",
        token_env="CONFLUENCE_TOKEN",
    )


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://wiki.example.org/confluence/pages/viewpage.action?pageId=12345",
            ("id", "12345"),
        ),
        (
            "https://wiki.example.org/confluence/pages/67890/Page-Title",
            ("id", "67890"),
        ),
        (
            "https://wiki.example.org/confluence/display/DEV/My+Page",
            ("title", ("DEV", "My Page")),
        ),
    ],
)
def test_confluence_page_reference_supported_forms(url, expected) -> None:
    assert _confluence_page_reference(
        url,
        _confluence_provider(),
    ) == expected


def test_confluence_page_reference_rejects_invalid_page_id() -> None:
    with pytest.raises(ValueError, match="numerisch"):
        _confluence_page_reference(
            "https://wiki.example.org/confluence/pages/viewpage.action?pageId=abc",
            _confluence_provider(),
        )


def test_confluence_api_url_for_id_and_title() -> None:
    provider = _confluence_provider()

    by_id = _confluence_api_url(provider, ("id", "123"))
    by_title = _confluence_api_url(
        provider,
        ("title", ("DEV", "My Page")),
    )

    assert "/rest/api/content/123?" in by_id
    assert "expand=body.view%2Cbody.storage" in by_id
    assert "/rest/api/content?" in by_title
    assert "spaceKey=DEV" in by_title
    assert "title=My+Page" in by_title


def test_confluence_document_accepts_single_search_result() -> None:
    title, content = _confluence_document(
        {
            "results": [
                {
                    "title": "API Docs",
                    "body": {
                        "view": {
                            "value": "<h1>Users API</h1><p>GET /users</p>"
                        }
                    },
                }
            ]
        }
    )

    assert title == "API Docs"
    assert "Users API" in content
    assert "GET /users" in content


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"results": []}, "nicht gefunden"),
        (
            {"results": [{"title": "A"}, {"title": "B"}]},
            "nicht eindeutig",
        ),
        (
            {"results": ["invalid"]},
            "ungültiges Seitenergebnis",
        ),
        (
            {"title": "No body"},
            "keinen Seiteninhalt",
        ),
        (
            {"title": "Empty", "body": {"view": {"value": "   "}}},
            "keinen verwertbaren Seiteninhalt",
        ),
    ],
)
def test_confluence_document_rejects_invalid_payloads(payload, message) -> None:
    with pytest.raises(ValueError, match=message):
        _confluence_document(payload)


def test_json_repairs_accumulate_main_usage(tmp_path: Path) -> None:
    class JsonRepairUsageModel(UsageRecordingModel):
        def __init__(self) -> None:
            super().__init__(
                [
                    TokenUsage(
                        input_tokens=100,
                        output_tokens=10,
                        total_tokens=110,
                    ),
                    TokenUsage(
                        input_tokens=200,
                        output_tokens=20,
                        total_tokens=220,
                    ),
                ]
            )
            self.responses = ["not json", '{"ok":true}']

        async def chat(self, messages, tools, **kwargs):
            self.calls.append((messages.copy(), tools.copy()))
            self.last_usage = self.pending_usages.pop(0)
            self.usage_history.append(self.last_usage)
            return {
                "role": "assistant",
                "content": self.responses.pop(0),
            }

    model = JsonRepairUsageModel()
    agent = WebContextCliAgent(
        tmp_path,
        model,
        (),
        response_format="json",
    )
    agent._exit_stack = SimpleNamespace()

    assert asyncio.run(agent.ask("Antworte als JSON.")) == '{"ok":true}'

    usage = agent._last_main_usage
    assert usage == LoopTokenUsage(
        requests=2,
        usage_requests=2,
        input_tokens=300,
        output_tokens=30,
        total_tokens=330,
        max_input_tokens=200,
        last_input_tokens=200,
    )
    tokens = asyncio.run(agent.ask("tokens"))
    assert "Modellaufrufe: 2" in tokens
    assert "Input gesamt: 300 Tokens" in tokens
    assert "Output gesamt: 30 Tokens" in tokens
    assert "Tokens gesamt: 330 Tokens" in tokens
