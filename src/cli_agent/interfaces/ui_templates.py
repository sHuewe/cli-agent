"""Jinja2 rendering of the built-in browser UI and a single project override."""
from __future__ import annotations

from collections import Counter
from html.parser import HTMLParser
from importlib.resources import files
from re import findall

from cli_agent.settings.config import UiConfig


class _UiStructure(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: Counter[str] = Counter()
        self.commands: Counter[str] = Counter()
        self.scripts: Counter[str] = Counter()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if values.get("id"):
            self.ids[values["id"]] += 1
        if values.get("data-command"):
            self.commands[values["data-command"]] += 1
        if tag == "script" and values.get("src"):
            self.scripts[values["src"]] += 1


def _validate_rendered_ui(html: str) -> None:
    """Fail closed if a project override removes JavaScript-owned elements."""
    js = files("cli_agent.interfaces").joinpath("static", "app.js").read_text(encoding="utf-8")
    required_ids = set(findall(r'getElementById\\("([^"]+)"\\)', js))
    required_ids.update({"language-select", "commands", "session-meta"})
    dom = _UiStructure()
    dom.feed(html)
    for name in required_ids:
        if dom.ids[name] != 1:
            raise ValueError(f"UI template must render exactly one element with id={name!r}.")
    for name in ("tokens", "add_web_context", "clear_web_context", "enable", "disable"):
        if dom.commands[name] != 1:
            raise ValueError(f"UI template must retain data-command={name!r}.")
    for asset in ("/assets/i18n.js", "/assets/app.js"):
        if dom.scripts[asset] != 1:
            raise ValueError(f"UI template must retain the script {asset}.")


def render_ui_template(config: UiConfig, *, project_template: str | None = None) -> str:
    """Restrict Jinja loading to packaged templates and one validated local file."""
    from jinja2 import DictLoader, PackageLoader, PrefixLoader, StrictUndefined
    from jinja2.sandbox import SandboxedEnvironment

    loaders = {"cli_agent": PackageLoader("cli_agent.interfaces", "templates/cli_agent")}
    if project_template is not None:
        loaders["project"] = DictLoader({"index.html": project_template})
    environment = SandboxedEnvironment(
        loader=PrefixLoader(loaders),
        autoescape=True,
        undefined=StrictUndefined,
        auto_reload=False,
    )
    template = "project/index.html" if project_template is not None else "cli_agent/index.html"
    rendered = environment.get_template(template).render(ui=config)
    _validate_rendered_ui(rendered)
    return rendered
