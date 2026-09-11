from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli_agent.model import TokenUsage
from cli_agent.web_context import WebContext, _extract_web_content, _validate_web_url
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


def test_validate_web_url_allows_local_and_private_hosts() -> None:
    assert _validate_web_url("http://localhost:8080/docs") == (
        "http://localhost:8080/docs"
    )
    assert _validate_web_url("http://10.1.2.3/docs") == "http://10.1.2.3/docs"


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
    def __init__(self, usages: list[TokenUsage]) -> None:
        super().__init__()
        self.pending_usages = list(usages)
        self.usage_history: list[TokenUsage] = []
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


def test_add_web_context_is_session_command_not_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_fetch(url: str) -> WebContext:
        return example_context(url)

    monkeypatch.setattr("cli_agent.web_context_agent.fetch_web_context", fake_fetch)
    agent = make_agent(tmp_path)

    answer = asyncio.run(agent.ask("add_web_context http://localhost:8080/docs"))

    assert "Web-Kontext hinzugefügt" in answer
    assert len(agent._web_contexts) == 1
    assert agent.history == []


def test_web_context_is_added_to_working_message_but_not_history(tmp_path: Path) -> None:
    model = RecordingModel()
    agent = make_agent(tmp_path, model)
    agent._web_contexts.append(example_context())

    answer = asyncio.run(agent.ask("Welche API ist beschrieben?"))

    assert answer == "ok"
    current_user_message = model.calls[-1][0][-1]["content"]
    assert "http://localhost:8080/docs" in current_user_message
    assert "GET /users" in current_user_message
    assert "Welche API ist beschrieben?" in current_user_message
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
        input_tokens=350,
        output_tokens=30,
        total_tokens=380,
        max_input_tokens=250,
        last_input_tokens=250,
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
    assert "Input gesamt: 3.000 Tokens" in answer
    assert "Output gesamt: 200 Tokens" in answer
    assert "Max. Input eines Aufrufs: 3.000 Tokens" in answer
    assert "Knowledge-Loop:" in answer
    assert "Input gesamt: 1.500 Tokens" in answer
    assert len(model.calls) == calls_before


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
