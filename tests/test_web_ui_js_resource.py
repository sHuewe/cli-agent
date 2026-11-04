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

    english = source.split("en: Object.freeze({", 1)[1].split("}),\n    de:", 1)[0]
    german = source.split("de: Object.freeze({", 1)[1].split("\n    })", 1)[0]
    names = lambda block: set(re.findall(r'^\s*"([^"]+)":', block, re.MULTILINE))
    assert names(english) == names(german)
    assert len(names(english)) >= 70

    app_source = files("cli_agent.interfaces").joinpath("static", "app.js").read_text(encoding="utf-8")
    assert 'window.cliAgentI18n' in app_source
    assert 'cli-agent-language-change' in app_source
    assert "replaceState(null" in app_source  # Browser token handling is unchanged.


def test_browser_scripts_have_valid_javascript_syntax():
    import shutil
    import subprocess
    import pytest

    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is not installed")
    for filename in ("i18n.js", "app.js"):
        asset = files("cli_agent.interfaces").joinpath("static", filename)
        result = subprocess.run(
            [node, "--check", str(asset)],
            capture_output=True, text=True, check=False, timeout=20,
        )
        assert result.returncode == 0, result.stderr


def test_i18n_language_detection_switch_and_persistence_with_node():
    import shutil
    import subprocess
    import pytest
    from cli_agent.interfaces.web_ui_api import API_I18N_JS

    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is not installed")
    script = r"""
const vm = require("node:vm");
const fs = require("node:fs");
const assert = require("node:assert/strict");
const elements = {
  label: {dataset: {i18n: "session.new"}, textContent: ""},
  placeholder: {dataset: {i18nPlaceholder: "composer.placeholder"},
    setAttribute(name, value) { this[name] = value; }},
  aria: {dataset: {i18nAriaLabel: "commands.label"},
    setAttribute(name, value) { this[name] = value; }},
  select: {value: "", addEventListener(name, callback) { this.onChange = callback; }}
};
const document = {
  documentElement: {lang: ""},
  querySelectorAll(selector) {
    if (selector === "[data-i18n]") return [elements.label];
    if (selector === "[data-i18n-placeholder]") return [elements.placeholder];
    if (selector === "[data-i18n-aria-label]") return [elements.aria];
    return [];
  },
  getElementById(name) { return name === "language-select" ? elements.select : null; }
};
const events = {};
const store = {};
const window = {
  localStorage: {
    getItem(key) { return store[key] ?? null; },
    setItem(key, value) { store[key] = value; }
  },
  addEventListener(type, callback) { events[type] = callback; },
  dispatchEvent(event) { this.lastEvent = event; }
};
const sandbox = {
  window, document,
  navigator: {languages: ["fr-FR", "de-DE", "en-US"]},
  CustomEvent: class { constructor(type, options) { this.type = type; this.detail = options.detail; } }
};
vm.runInNewContext(fs.readFileSync(0, "utf8"), sandbox);
assert.equal(document.documentElement.lang, "de");
assert.equal(elements.label.textContent, "Neue Session in neuem Tab");
assert.equal(elements.select.value, "de");
assert.equal(elements.placeholder.placeholder, "Nachricht an cli-agent");
elements.select.onChange({target: {value: "en"}});
assert.equal(document.documentElement.lang, "en");
assert.equal(elements.label.textContent, "New session in new tab");
assert.equal(elements.aria["aria-label"], "Local commands");
assert.equal(store["cli-agent-ui-language"], "en");
assert.equal(window.lastEvent.type, "cli-agent-language-change");
assert.equal(window.cliAgentI18n.t("tools.group", {count: 3}), "Tool calls (3)");
events.storage({key: "cli-agent-ui-language", newValue: "de"});
assert.equal(document.documentElement.lang, "de");
assert.equal(store["cli-agent-ui-language"], "en");
window.cliAgentI18n.setLocale("unsupported");
assert.equal(document.documentElement.lang, "de");
"""
    result = subprocess.run(
        [node, "-e", script], input=API_I18N_JS, text=True,
        encoding="utf-8", capture_output=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_connection_status_survives_language_changes():
    """A disconnected or terminated session must never show stale metadata."""
    import shutil
    import subprocess
    import pytest
    from cli_agent.interfaces.web_ui import INDEX_HTML
    from cli_agent.interfaces.web_ui_api import API_APP_JS

    # Static locale application runs before app.js and must not overwrite a
    # runtime-owned status on later language switches.
    assert 'id="session-meta" class="meta" data-i18n=' not in INDEX_HTML
    start = API_APP_JS.index("  let currentSessionMetadata = null;")
    end = API_APP_JS.index("  function renderSelectedWorkingMessages()", start)
    status_logic = API_APP_JS[start:end]
    assert 'setSessionStatus("error.invalidLink")' in API_APP_JS
    assert 'setSessionStatus("session.ended")' in API_APP_JS
    assert 'setSessionStatus("session.bindingLost")' in API_APP_JS
    assert 'setSessionStatus("session.openFailed")' in API_APP_JS
    assert 'setSessionStatus("session.meta")' in API_APP_JS
    assert 'window.addEventListener("cli-agent-language-change", renderSessionMeta);' in API_APP_JS

    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is not installed")
    script = (
        '''
const assert = require("node:assert/strict");
const meta = {textContent: ""};
let locale = "en";
const translations = {
  en: {"connection.establishing": "Connecting", "error.invalidLink": "Invalid link",
    "session.ended": "Session ended", "session.bindingLost": "Lease lost",
    "session.openFailed": "Open failed", "session.noneMcp": "(none)"},
  de: {"connection.establishing": "Verbinden", "error.invalidLink": "Ungültiger Link",
    "session.ended": "Session beendet", "session.bindingLost": "Verbindung verloren",
    "session.openFailed": "Öffnen fehlgeschlagen", "session.noneMcp": "(keine)"}
};
function t(key, values = {}) {
  if (key === "session.meta") return (locale === "en" ? "Model: " : "Modell: ") + values.model;
  return translations[locale][key] ?? key;
}
''' + status_logic + '''
currentSessionMetadata = {workspace: "/test", model: "model-x", servers: ""};
setSessionStatus("session.meta");
assert.equal(meta.textContent, "Model: model-x");
for (const [key, en, de] of [
  ["session.ended", "Session ended", "Session beendet"],
  ["session.bindingLost", "Lease lost", "Verbindung verloren"],
  ["session.openFailed", "Open failed", "Öffnen fehlgeschlagen"],
  ["error.invalidLink", "Invalid link", "Ungültiger Link"]
]) {
  locale = "en"; setSessionStatus(key);
  assert.equal(meta.textContent, en);
  locale = "de"; renderSessionMeta();
  assert.equal(meta.textContent, de);
  locale = "en"; renderSessionMeta();
  assert.equal(meta.textContent, en);
}
setSessionStatus("session.meta");
locale = "de"; renderSessionMeta();
assert.equal(meta.textContent, "Modell: model-x");
console.log("ok");
'''
    )
    result = subprocess.run(
        [node, "-e", script], text=True, encoding="utf-8",
        capture_output=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
