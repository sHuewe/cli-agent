"""The API browser application is a packaged, standalone JavaScript resource."""
from importlib.resources import files
from cli_agent.interfaces.web_ui_api import API_APP_JS


def test_api_js_is_packaged_resource_without_transformation():
    source = files("cli_agent.interfaces").joinpath("static", "app.js").read_text(encoding="utf-8")
    assert source == API_APP_JS
    assert source.lstrip().startswith("(() => {")


def test_i18n_catalog_is_packaged_and_discovered_from_json():
    from cli_agent.interfaces.web_ui_api import API_I18N_JS
    from cli_agent.interfaces.web_ui import INDEX_HTML
    from cli_agent.interfaces.web_ui_locales import available_locales, get_locale

    source = files("cli_agent.interfaces").joinpath("static", "i18n.js").read_text(encoding="utf-8")
    assert source == API_I18N_JS
    assert '<script src="/assets/i18n.js" defer></script>' in INDEX_HTML
    assert INDEX_HTML.index("/assets/i18n.js") < INDEX_HTML.index("/assets/app.js")
    assert 'id="language-select"' in INDEX_HTML
    assert 'value="de"' not in INDEX_HTML  # Languages come from the manifest.
    assert "window.localStorage.setItem(STORAGE_KEY, locale)" in source
    assert "document.documentElement.lang = locale" in source
    assert "locales.json" in source
    assert "new Option(displayName(code), code)" in source
    assert "ready: initialize()" in source
    assert "const messages = Object.freeze({" not in source  # No embedded translation catalog.
    assert {"en", "de"}.issubset(available_locales())
    english, german = get_locale("en"), get_locale("de")
    assert english and german
    assert english.keys() == german.keys()
    assert len(english) >= 70
    assert english["session.new"] == "New session in new tab"
    assert german["session.new"] == "Neue Session in neuem Tab"
    assert get_locale("../de") is None
    assert get_locale("NOT-A-LANGUAGE") is None
    # Adding fr.json later must not require changing this test.

    app_source = files("cli_agent.interfaces").joinpath("static", "app.js").read_text(encoding="utf-8")
    assert "window.cliAgentI18n" in app_source
    assert "void i18n.ready.then(() => {" in app_source
    assert "cli-agent-language-change" in app_source
    assert "replaceState(null" in app_source  # Browser token handling is unchanged.


def test_new_locale_json_becomes_available_without_code_changes(monkeypatch, tmp_path):
    import json
    from cli_agent.interfaces import web_ui_locales

    (tmp_path / "en.json").write_text('{"hello": "Hello"}', encoding="utf-8")
    (tmp_path / "fr.json").write_text('{"hello": "Bonjour"}', encoding="utf-8")
    (tmp_path / "junk.json").write_text("invalid JSON", encoding="utf-8")
    (tmp_path / "evil..json").write_text('{"hello": "bad"}', encoding="utf-8")
    monkeypatch.setattr(web_ui_locales, "_catalog_dir", lambda: tmp_path)
    assert web_ui_locales.available_locales() == ["en", "fr"]
    assert web_ui_locales.get_locale("fr") == {"hello": "Bonjour"}
    assert web_ui_locales.get_locale("fr-FR") is None
    assert web_ui_locales.get_locale("../en") is None
    (tmp_path / "fr.json").write_text('{"hello": 123}', encoding="utf-8")
    assert web_ui_locales.available_locales() == ["en"]


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
    import json
    import shutil
    import subprocess
    import pytest
    from cli_agent.interfaces.web_ui_api import API_I18N_JS
    from cli_agent.interfaces.web_ui_locales import get_locale

    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is not installed")
    catalogs = {
        "en": get_locale("en"), "de": get_locale("de"),
        "fr": {"session.new": "Nouvelle session"},
    }
    script = "const catalogs = " + json.dumps(catalogs, ensure_ascii=True) + ";\n" + r"""
const vm = require("node:vm");
const fs = require("node:fs");
const assert = require("node:assert/strict");
const elements = {
  label: {dataset: {i18n: "session.new"}, textContent: ""},
  placeholder: {dataset: {i18nPlaceholder: "composer.placeholder"},
    setAttribute(name, value) { this[name] = value; }},
  aria: {dataset: {i18nAriaLabel: "commands.label"},
    setAttribute(name, value) { this[name] = value; }},
  select: {
    value: "", options: [],
    replaceChildren() { this.options = []; },
    add(option) { this.options.push(option); },
    addEventListener(name, callback) { this.onChange = callback; }
  }
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
const requests = [];
async function fetch(url) {
  requests.push(url);
  if (url === "/assets/i18n/locales.json") {
    return {ok: true, json: async () => ["en", "de", "fr"]};
  }
  const name = url.replace("/assets/i18n/", "").replace(".json", "");
  if (!Object.hasOwn(catalogs, name)) return {ok: false};
  return {ok: true, json: async () => catalogs[name]};
}
const sandbox = {
  window, document, fetch,
  Option: class { constructor(label, value) { this.label = label; this.value = value; } },
  navigator: {languages: ["fr-FR", "de-DE", "en-US"]},
  CustomEvent: class { constructor(type, options) { this.type = type; this.detail = options.detail; } }
};
vm.runInNewContext(fs.readFileSync(0, "utf8"), sandbox);
(async () => {
  await window.cliAgentI18n.ready;
  assert.equal(document.documentElement.lang, "fr");
  assert.deepEqual(elements.select.options.map(x => x.value), ["en", "de", "fr"]);
  assert.equal(elements.label.textContent, "Nouvelle session");
  assert.equal(elements.placeholder.placeholder, "Message to cli-agent"); // Fallback for missing fr key.
  assert.equal(elements.select.value, "fr");
  await window.cliAgentI18n.setLocale("en");
  assert.equal(document.documentElement.lang, "en");
  assert.equal(elements.label.textContent, "New session in new tab");
  assert.equal(elements.aria["aria-label"], "Local commands");
  assert.equal(store["cli-agent-ui-language"], "en");
  assert.equal(window.lastEvent.type, "cli-agent-language-change");
  assert.equal(window.cliAgentI18n.t("tools.group", {count: 3}), "Tool calls (3)");
  events.storage({key: "cli-agent-ui-language", newValue: "de-DE"});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(document.documentElement.lang, "de");
  assert.equal(store["cli-agent-ui-language"], "en"); // Storage event does not persist.
  assert.equal(elements.label.textContent, "Neue Session in neuem Tab");
  const accepted = await window.cliAgentI18n.setLocale("unsupported");
  assert.equal(accepted, false);
  assert.equal(document.documentElement.lang, "de");
  assert(requests.includes("/assets/i18n/fr.json"));
  console.log("ok");
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run(
        [node, "-e", script], input=API_I18N_JS, text=True,
        encoding="utf-8", capture_output=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


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
const localeListeners = {};
const window = {addEventListener(type, callback) { localeListeners[type] = callback; }};
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
assert.equal(meta.textContent, "Connecting");
assert.equal(typeof localeListeners["cli-agent-language-change"], "function");
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
  locale = "de"; localeListeners["cli-agent-language-change"]();
  assert.equal(meta.textContent, de);
  locale = "en"; localeListeners["cli-agent-language-change"]();
  assert.equal(meta.textContent, en);
}
setSessionStatus("session.meta");
locale = "de"; localeListeners["cli-agent-language-change"]();
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
