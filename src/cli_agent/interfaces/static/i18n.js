/* Browser UI localization. Language catalogs are discovered at runtime. */
(() => {
  "use strict";
  const STORAGE_KEY = "cli-agent-ui-language";
  const BASE_URL = "/assets/i18n/";
  const messages = new Map();
  let supported = ["en"];
  let locale = "en";
  let loadGeneration = 0;
  const select = document.getElementById("language-select");

  function normalize(value) {
    const language = String(value || "").trim().toLowerCase().replaceAll("_", "-");
    return /^[a-z]{2,3}(?:-[a-z0-9]{2,8})*$/.test(language) ? language : null;
  }

  function matchLocale(value) {
    const candidate = normalize(value);
    if (!candidate) return null;
    if (supported.includes(candidate)) return candidate;
    const base = candidate.split("-")[0];
    return supported.includes(base) ? base : null;
  }

  function browserLocale() {
    for (const value of (navigator.languages || [navigator.language || "en"])) {
      const chosen = matchLocale(value);
      if (chosen) return chosen;
    }
    return "en";
  }

  async function fetchCatalog(code) {
    if (messages.has(code)) return messages.get(code);
    const response = await fetch(BASE_URL + encodeURIComponent(code) + ".json", {cache: "no-store"});
    if (!response.ok) throw new Error("Unable to load locale: " + code);
    const payload = await response.json();
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
      throw new Error("Invalid locale catalog: " + code);
    }
    // Keep translated strings as text; never interpret them as HTML.
    if (!Object.values(payload).every(value => typeof value === "string")) {
      throw new Error("Invalid locale values: " + code);
    }
    messages.set(code, payload);
    return payload;
  }

  function t(key, values = {}) {
    const selected = messages.get(locale) || {};
    const fallback = messages.get("en") || {};
    const message = selected[key] ?? fallback[key];
    if (message === undefined) return key;
    return message.replace(/\{([a-zA-Z][a-zA-Z0-9_]*)\}/g, (match, name) =>
      Object.hasOwn(values, name) ? String(values[name]) : match
    );
  }

  function displayName(code) {
    try {
      const names = new Intl.DisplayNames([code], {type: "language"});
      return names.of(code) || code;
    } catch (_error) {
      return code;
    }
  }

  function populateSelector() {
    if (!select) return;
    select.replaceChildren();
    for (const code of supported) {
      select.add(new Option(displayName(code), code));
    }
    select.value = locale;
  }

  function apply() {
    document.documentElement.lang = locale;
    for (const node of document.querySelectorAll("[data-i18n]")) {
      const key = node.dataset.i18n;
      // Do not discard the English HTML if even the fallback catalog failed.
      if (messages.get(locale)?.[key] !== undefined || messages.get("en")?.[key] !== undefined) {
        node.textContent = t(key);
      }
    }
    for (const node of document.querySelectorAll("[data-i18n-placeholder]")) {
      const key = node.dataset.i18nPlaceholder;
      if (messages.get(locale)?.[key] !== undefined || messages.get("en")?.[key] !== undefined) {
        node.setAttribute("placeholder", t(key));
      }
    }
    for (const node of document.querySelectorAll("[data-i18n-aria-label]")) {
      const key = node.dataset.i18nAriaLabel;
      if (messages.get(locale)?.[key] !== undefined || messages.get("en")?.[key] !== undefined) {
        node.setAttribute("aria-label", t(key));
      }
    }
    if (select) select.value = locale;
  }

  async function setLocale(next, persist = true) {
    const chosen = matchLocale(next);
    if (!chosen) return false;
    const generation = ++loadGeneration;
    try {
      await fetchCatalog(chosen);
    } catch (_error) {
      return false;
    }
    if (generation !== loadGeneration) return false;
    const changed = chosen !== locale;
    locale = chosen;
    if (persist) {
      try { window.localStorage.setItem(STORAGE_KEY, locale); }
      catch (_error) { /* Private mode may prohibit storage. */ }
    }
    apply();
    if (changed) {
      window.dispatchEvent(new CustomEvent("cli-agent-language-change", {detail: {locale}}));
    }
    return true;
  }

  async function initialize() {
    try {
      const response = await fetch(BASE_URL + "locales.json", {cache: "no-store"});
      if (!response.ok) throw new Error("Locale discovery failed");
      const found = await response.json();
      if (!Array.isArray(found)) throw new Error("Invalid locale manifest");
      supported = [...new Set(found.map(normalize).filter(Boolean))];
      if (!supported.includes("en")) throw new Error("Missing English fallback locale");
    } catch (_error) {
      supported = ["en"];
    }
    // Fetch the English fallback first, then the preferred language.
    try { await fetchCatalog("en"); }
    catch (_error) { /* Static HTML remains in English. */ }
    let saved = null;
    try { saved = window.localStorage.getItem(STORAGE_KEY); }
    catch (_error) { /* Storage may be unavailable. */ }
    const preferred = matchLocale(saved) || browserLocale();
    if (preferred !== "en") {
      try { await fetchCatalog(preferred); locale = preferred; }
      catch (_error) { locale = "en"; }
    }
    populateSelector();
    apply();
    if (select) {
      select.addEventListener("change", event => {
        void setLocale(event.target.value).then(success => {
          if (!success) select.value = locale;
        });
      });
    }
    window.addEventListener("storage", event => {
      if (event.key === STORAGE_KEY) void setLocale(event.newValue || browserLocale(), false);
    });
  }

  window.cliAgentI18n = Object.freeze({
    t, apply, setLocale, getLocale: () => locale,
    getSupportedLocales: () => [...supported],
    ready: initialize()
  });
})();
