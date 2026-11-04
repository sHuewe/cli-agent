"""The API browser application is a packaged, standalone JavaScript resource."""
from importlib.resources import files
from cli_agent.interfaces.web_ui_api import API_APP_JS


def test_api_js_is_packaged_resource_without_transformation():
    source = files("cli_agent.interfaces").joinpath("static", "app.js").read_text(encoding="utf-8")
    assert source == API_APP_JS
    assert source.lstrip().startswith("(() => {")


def test_i18n_catalog_is_packaged_and_has_both_languages():
    from cli_agent.interfaces.web_ui_api import API_I18N_JS
    from cli_agent.interfaces.web_ui import INDEX_HTML
    import re

    source = files("cli_agent.interfaces").joinpath("static", "i18n.js").read_text(encoding="utf-8")
    assert source == API_I18N_JS
    assert '<script src="/assets/i18n.js" defer></script>' in INDEX_HTML
    assert INDEX_HTML.index("/assets/i18n.js") < INDEX_HTML.index("/assets/app.js")
    assert 'id="language-select"' in INDEX_HTML
    assert 'value="en"' in INDEX_HTML and 'value="de"' in INDEX_HTML
    assert 'window.localStorage.setItem(STORAGE_KEY, locale)' in source
    assert 'document.documentElement.lang = locale' in source

    english = source.split("en: Object.freeze({", 1)[1].split("}),\\n    de:", 1)[0]
    german = source.split("de: Object.freeze({", 1)[1].split("\\n    })", 1)[0]
    names = lambda block: set(re.findall(r'^\\s*"([^"]+)":', block, re.MULTILINE))
    assert names(english) == names(german)
    assert len(names(english)) >= 70

    app_source = files("cli_agent.interfaces").joinpath("static", "app.js").read_text(encoding="utf-8")
    assert 'window.cliAgentI18n' in app_source
    assert 'cli-agent-language-change' in app_source
    assert "replaceState(null" in app_source  # Browser token handling is unchanged.
