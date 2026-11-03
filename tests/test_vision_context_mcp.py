from __future__ import annotations

import inspect
from cli_agent.platform import os_mcp_server
from cli_agent.platform.vision_analysis import VisionConfig
from cli_agent.settings.config import McpServerConfig


class FakeFastMCP:
    def __init__(self, *_args, **_kwargs):
        self.tools = {}

    def tool(self):
        def register(fn):
            self.tools[fn.__name__] = fn
            return fn
        return register


def test_mcp_image_tool_exposes_both_context_options(monkeypatch):
    monkeypatch.setattr(os_mcp_server, "FastMCP", FakeFastMCP)
    calls = []

    def fake_vision(workspace, path, question, vision, *, context, context_path):
        calls.append((path, question, context, context_path))
        return "ok"

    monkeypatch.setattr(os_mcp_server, "analyze_workspace_image", fake_vision)
    server = os_mcp_server.create_server(
        object(), McpServerConfig(name="os"),
        vision_config=VisionConfig("vision", "http://localhost:9999/v1", None),
    )
    tool = server.tools["analyze_image"]
    assert list(inspect.signature(tool).parameters) == [
        "path", "question", "context", "context_path"
    ]
    assert tool("image.png", "What?", "details", "docs/context.md") == "ok"
    assert tool("image.png", "What?") == "ok"
    assert calls == [
        ("image.png", "What?", "details", "docs/context.md"),
        ("image.png", "What?", None, None),
    ]
