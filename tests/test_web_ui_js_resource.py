"""The API browser application is a packaged, standalone JavaScript resource."""
from importlib.resources import files
from cli_agent.interfaces.web_ui_api import API_APP_JS


def test_api_js_is_packaged_resource_without_transformation():
    source = files("cli_agent.interfaces").joinpath("static", "app.js").read_text(encoding="utf-8")
    assert source == API_APP_JS
    assert source.startswith("\\n(() => {") or source.startswith("(() => {")
