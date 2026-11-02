from __future__ import annotations

import base64
import json

import httpx
import pytest

from cli_agent.platform.os_operations import Workspace, WorkspaceError
from cli_agent.platform import vision_analysis
from cli_agent.settings.config import McpServerConfig
from cli_agent.runtime.agent import CliAgent


def _workspace(tmp_path):
    return Workspace.from_directory(tmp_path, McpServerConfig(name="os"))


def _png():
    return b"\x89PNG\r\n\x1a\n" + b"sample-image-data"


def test_image_uses_question_and_returns_model_answer(tmp_path, monkeypatch):
    (tmp_path / "screen.png").write_bytes(_png())
    captured = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured["client"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def stream(self, method, url, *, headers, json):
            captured.update(method=method, url=url, headers=headers, payload=json)

            class Response:
                status_code = 200
                def __enter__(self):
                    return self
                def __exit__(self, *_args):
                    return False
                def iter_bytes(self):
                    yield b'{"choices":[{"message":{"content":"An error message is shown."}}]}'
            return Response()

    monkeypatch.setattr(vision_analysis.httpx, "Client", FakeClient)
    result = vision_analysis.analyze_image(
        _workspace(tmp_path),
        "screen.png",
        "Welche Fehlermeldung wird angezeigt?",
        vision_analysis.VisionConfig("vision-model", "https://openrouter.ai/api/v1", "secret"),
    )
    assert result == "An error message is shown."
    assert captured["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert captured["method"] == "POST"
    assert captured["client"]["trust_env"] is False
    assert captured["client"]["follow_redirects"] is False
    assert captured["payload"]["messages"][0]["content"][0]["text"] == "Welche Fehlermeldung wird angezeigt?"
    image_url = captured["payload"]["messages"][0]["content"][1]["image_url"]["url"]
    assert image_url == "data:image/png;base64," + base64.b64encode(_png()).decode("ascii")


@pytest.mark.parametrize("path,data", [
    ("screen.txt", _png()),
    ("screen.png", b"not-png"),
    ("screen.jpg", _png()),
])
def test_rejects_unsupported_or_mismatched_images(tmp_path, path, data):
    (tmp_path / path).write_bytes(data)
    with pytest.raises(WorkspaceError):
        vision_analysis._read_image(_workspace(tmp_path), path)


def test_rejects_protected_and_unsafe_image_paths(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "a.png").write_bytes(_png())
    (tmp_path / "a.png").write_bytes(_png())
    (tmp_path / "alias.png").symlink_to("a.png")
    for path in ("../a.png", ".git/a.png", "alias.png"):
        with pytest.raises(WorkspaceError):
            vision_analysis._read_image(_workspace(tmp_path), path)


def test_rejects_large_file_and_invalid_question(tmp_path):
    (tmp_path / "large.png").write_bytes(_png() + b"x" * vision_analysis.MAX_IMAGE_BYTES)
    with pytest.raises(WorkspaceError, match="Limit"):
        vision_analysis._read_image(_workspace(tmp_path), "large.png")
    (tmp_path / "good.png").write_bytes(_png())
    with pytest.raises(WorkspaceError, match="question"):
        vision_analysis.analyze_image(
            _workspace(tmp_path), "good.png", "",
            vision_analysis.VisionConfig("m", "http://localhost:1234/v1", None),
        )


def test_vision_disabled_without_model(monkeypatch):
    monkeypatch.delenv("VISION_MODEL", raising=False)
    assert vision_analysis.vision_config_from_environment() is None


def test_vision_rejects_unapproved_remote_host(monkeypatch):
    monkeypatch.setenv("VISION_MODEL", "a-vision-model")
    monkeypatch.setenv("VISION_BASE_URL", "https://unapproved.example/v1")
    monkeypatch.setenv("VISION_API_KEY", "not-real")
    with pytest.raises(ValueError, match="nicht erlaubt"):
        vision_analysis.vision_config_from_environment()


def test_vision_remote_host_requires_approved_credential(monkeypatch):
    from cli_agent.security.network_policy import NetworkConfig
    from cli_agent.settings.admin_config import AdminConfig
    monkeypatch.setenv("VISION_MODEL", "a-vision-model")
    monkeypatch.setenv("VISION_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("VISION_API_KEY", "not-real")
    monkeypatch.setattr(
        vision_analysis, "load_admin_config",
        lambda: AdminConfig(network=NetworkConfig(model_allowed_hosts=("openrouter.ai",))),
    )
    with pytest.raises(PermissionError, match="nicht administrativ freigegeben"):
        vision_analysis.vision_config_from_environment()


def test_built_in_image_tool_requires_approval(tmp_path):
    class Model:
        model = "test"
        base_url = "local://test"
    agent = CliAgent(tmp_path, Model(), ())
    config = McpServerConfig(name="os", built_in=True, config={"allow_write_files": False})
    assert agent._requires_approval(config, "read_file", "os__read_file") is False
    assert agent._requires_approval(config, "analyze_image", "os__analyze_image") is True
