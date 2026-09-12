from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli_agent.web_context import WebContext, _extract_web_content, _validate_web_url
from cli_agent.web_context_agent import WebContextCliAgent


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
    async def fake_fetch(url: str, **_: object) -> WebContext:
        return example_context(url)

    monkeypatch.setattr("cli_agent.web_context_agent.fetch_web_context", fake_fetch)
    agent = make_agent(tmp_path)

    answer = asyncio.run(agent.ask("add_web_context http://localhost:8080/docs"))

    assert "Web-Kontext hinzugefügt" in answer
    assert len(agent._web_contexts) == 1
    assert agent.history == []


def test_web_context_is_added_to_working_message_but_not_history(
    tmp_path: Path,
) -> None:
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
