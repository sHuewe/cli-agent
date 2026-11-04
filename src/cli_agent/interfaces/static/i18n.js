/* Browser-only UI translations: never translate agent prompts, API identifiers or model output. */
(() => {
  "use strict";
  const STORAGE_KEY = "cli-agent-ui-language";
  const messages = Object.freeze({
    en: Object.freeze({
      "language.label": "Language",
      "connection.establishing": "Connecting …",
      "session.new": "New session in new tab",
      "session.list": "Sessions",
      "session.end": "End session",
      "session.ended": "Session ended. Other sessions remain active. Open a new session in a new tab.",
      "session.description": "Each session can only be open in one active browser tab. Available sessions may be reopened or ended.",
      "session.currentTab": "current tab",
      "session.occupied": "open",
      "session.available": "available",
      "session.open": "Open",
      "session.close": "End",
      "session.confirmEnd": "End this session permanently? Other sessions will remain open.",
      "session.bindingLost": "Session lease lost. Reopen this tab.",
      "session.openFailed": "Could not open the session.",
      "session.noneMcp": "(none)",
      "session.meta": "Workspace: {workspace} · Model: {model} · MCP: {servers}",
      "history.reset": "Reset history",
      "history.confirmReset": "Really reset the conversation history?",
      "history.wasReset": "History has been reset.",
      "context.open": "LLM Context",
      "context.description": "Messages from the last main loop, plus currently active tools and reference contexts.",
      "context.runLabel": "Agent run:",
      "context.mainRun": "Main run",
      "context.subRun": "Sub-run: {task}",
      "context.cleanupRun": "Cleanup #{number} – {cause}",
      "context.planChanged": "Plan changed",
      "context.toolLimit": "20 tool calls",
      "context.messages": "Messages",
      "context.tools": "Tools",
      "context.contexts": "Context",
      "context.noTools": "No MCP tools available.",
      "context.noContexts": "No file or web contexts available.",
      "context.okfReady": "Configured knowledge run before the main request.",
      "context.okfUnavailable": "Configured but currently unavailable.",
      "context.fileSource": "File: {source}",
      "context.webSource": "Web: {source}",
      "context.inactive": "Web UI connection is not active.",
      "toggle.enable": "Enable",
      "toggle.disable": "Disable",
      "toggle.mcpDisabled": "MCP server is disabled.",
      "commands.label": "Local commands",
      "commands.tokens": "Tokens",
      "commands.addWeb": "Add web context",
      "commands.clearWeb": "Clear web context",
      "commands.enableMcp": "Enable MCP",
      "commands.disableMcp": "Disable MCP",
      "commands.cancel": "Cancel",
      "commands.webQuestion": "Which URL should be added as web context?",
      "commands.enableQuestion": "Which MCP server should be enabled?",
      "commands.disableQuestion": "Which MCP server should be disabled?",
      "commands.serverPlaceholder": "MCP server name",
      "commands.cancelled": "Command cancelled.",
      "composer.placeholder": "Message to cli-agent",
      "composer.send": "Send",
      "composer.stop": "Stop",
      "actions.close": "Close",
      "actions.refresh": "Refresh",
      "loading": "Loading …",
      "approval.title": "Tool approval required",
      "approval.diff": "Change preview",
      "approval.no": "No",
      "approval.session": "For session",
      "approval.yes": "Yes",
      "approval.fallback": "Tool approval",
      "tools.group": "Tool calls ({count})",
      "tools.running": "Running …",
      "tools.success": "Succeeded",
      "tools.error": "Error",
      "tools.denied": "Denied",
      "tools.approval": "Approval required",
      "tools.finished": "Finished",
      "vector.summary": "{count} matches · Collection: {collection} · {phase}",
      "vector.initial": "Initial search",
      "vector.next": "Further search",
      "download.button": "Download: {filename}",
      "download.failed": "Download failed: {error}",
      "cleanup.running": "Running",
      "cleanup.cancelled": "Cancelled",
      "cleanup.applied": "Applied",
      "cleanup.unchanged": "No change",
      "cleanup.invalid": "Invalid response",
      "cleanup.failed": "Failed",
      "cleanup.unknown": "Unknown",
      "cleanup.processing": "Processing: {status}",
      "cleanup.evidences": "Evidence: {before} → {after}",
      "cleanup.size": "Size: {before} → {after} bytes",
      "cleanup.changes": "Removed: {removed} · Updated: {updated} · Merged: {merged}",
      "cleanup.error": "Error: {error}",
      "error.invalidLink": "Invalid start link: missing session token.",
      "error.runFailed": "Agent run failed.",
      "error.streamEnded": "Agent event stream ended unexpectedly.",
      "error.messageTooLarge": "The current message or tool continuation is too large. Please shorten the content.",
      "error.lastMessageTooLarge": "The latest message exceeds the AG-UI limit.",
      "error.invalidPrompt": "Invalid prompt.",
      "error.invalidCommand": "Invalid local command.",
      "error.invalidServerResponse": "Invalid response from the local Web UI server.",
      "error.unknown": "Unknown error.",
      "error.popupBlocked": "The browser blocked opening a new tab."
    }),
    de: Object.freeze({
      "language.label": "Sprache",
      "connection.establishing": "Verbindung wird hergestellt …",
      "session.new": "Neue Session in neuem Tab",
      "session.list": "Sessions",
      "session.end": "Session beenden",
      "session.ended": "Session beendet. Andere Sessions laufen weiter. Neue Session in neuem Tab öffnen.",
      "session.description": "Jede Session kann nur in einem aktiven Browser-Tab geöffnet sein. Freie Sessions lassen sich erneut öffnen oder beenden.",
      "session.currentTab": "aktueller Tab",
      "session.occupied": "geöffnet",
      "session.available": "frei",
      "session.open": "Öffnen",
      "session.close": "Beenden",
      "session.confirmEnd": "Diese Session dauerhaft beenden? Andere Sessions bleiben geöffnet.",
      "session.bindingLost": "Session-Bindung verloren. Tab erneut öffnen.",
      "session.openFailed": "Session konnte nicht geöffnet werden.",
      "session.noneMcp": "(keine)",
      "session.meta": "Workspace: {workspace} · Modell: {model} · MCP: {servers}",
      "history.reset": "History zurücksetzen",
      "history.confirmReset": "Conversation-History wirklich zurücksetzen?",
      "history.wasReset": "History wurde zurückgesetzt.",
      "context.open": "LLM Context",
      "context.description": "Messages des letzten Main-Loops sowie aktuell wirksame Tools und Referenzkontexte.",
      "context.runLabel": "Agentenlauf:",
      "context.mainRun": "Hauptlauf",
      "context.subRun": "Sub-Run: {task}",
      "context.cleanupRun": "Cleanup #{number} – {cause}",
      "context.planChanged": "Planänderung",
      "context.toolLimit": "20 Toolaufrufe",
      "context.messages": "Messages",
      "context.tools": "Tools",
      "context.contexts": "Context",
      "context.noTools": "Keine MCP-Tools verfügbar.",
      "context.noContexts": "Keine Datei- oder Web-Kontexte vorhanden.",
      "context.okfReady": "Konfigurierter Knowledge-Lauf vor der Hauptanfrage.",
      "context.okfUnavailable": "Konfiguriert, aber aktuell nicht verfügbar.",
      "context.fileSource": "Datei: {source}",
      "context.webSource": "Web: {source}",
      "context.inactive": "Web-UI-Verbindung ist nicht aktiv.",
      "toggle.enable": "Aktivieren",
      "toggle.disable": "Deaktivieren",
      "toggle.mcpDisabled": "MCP-Server ist deaktiviert.",
      "commands.label": "Lokale Befehle",
      "commands.tokens": "Tokens",
      "commands.addWeb": "Web-Kontext hinzufügen",
      "commands.clearWeb": "Web-Kontext löschen",
      "commands.enableMcp": "MCP aktivieren",
      "commands.disableMcp": "MCP deaktivieren",
      "commands.cancel": "Abbrechen",
      "commands.webQuestion": "Welche URL soll als Web-Kontext hinzugefügt werden?",
      "commands.enableQuestion": "Welcher MCP-Server soll aktiviert werden?",
      "commands.disableQuestion": "Welcher MCP-Server soll deaktiviert werden?",
      "commands.serverPlaceholder": "Name des MCP-Servers",
      "commands.cancelled": "Befehl abgebrochen.",
      "composer.placeholder": "Nachricht an cli-agent",
      "composer.send": "Senden",
      "composer.stop": "Stop",
      "actions.close": "Schließen",
      "actions.refresh": "Aktualisieren",
      "loading": "Wird geladen …",
      "approval.title": "Tool-Freigabe erforderlich",
      "approval.diff": "Änderungsvorschau",
      "approval.no": "Nein",
      "approval.session": "Für Session",
      "approval.yes": "Ja",
      "approval.fallback": "Tool-Freigabe",
      "tools.group": "Tool-Aufrufe ({count})",
      "tools.running": "Läuft …",
      "tools.success": "Erfolgreich",
      "tools.error": "Fehler",
      "tools.denied": "Abgelehnt",
      "tools.approval": "Freigabe nötig",
      "tools.finished": "Beendet",
      "vector.summary": "{count} Treffer · Collection: {collection} · {phase}",
      "vector.initial": "Initiale Suche",
      "vector.next": "Weitere Suche",
      "download.button": "Herunterladen: {filename}",
      "download.failed": "Download fehlgeschlagen: {error}",
      "cleanup.running": "Läuft",
      "cleanup.cancelled": "Abgebrochen",
      "cleanup.applied": "Angewendet",
      "cleanup.unchanged": "Keine Änderung",
      "cleanup.invalid": "Ungültige Antwort",
      "cleanup.failed": "Fehlgeschlagen",
      "cleanup.unknown": "Unbekannt",
      "cleanup.processing": "Verarbeitung: {status}",
      "cleanup.evidences": "Evidences: {before} → {after}",
      "cleanup.size": "Größe: {before} → {after} Bytes",
      "cleanup.changes": "Entfernt: {removed} · Aktualisiert: {updated} · Zusammengeführt: {merged}",
      "cleanup.error": "Fehler: {error}",
      "error.invalidLink": "Ungültiger Start-Link: Session-Token fehlt.",
      "error.runFailed": "Agentenlauf fehlgeschlagen.",
      "error.streamEnded": "Agenten-Eventstream vorzeitig beendet.",
      "error.messageTooLarge": "Die aktuelle Nachricht oder Tool-Fortsetzung ist zu groß. Bitte den Inhalt verkürzen.",
      "error.lastMessageTooLarge": "Letzte Nachricht überschreitet das AG-UI-Limit.",
      "error.invalidPrompt": "Ungültiger Prompt.",
      "error.invalidCommand": "Ungültiger lokaler Befehl.",
      "error.invalidServerResponse": "Ungültige Antwort vom lokalen Web-UI-Server.",
      "error.unknown": "Unbekannter Fehler.",
      "error.popupBlocked": "Browser hat das Öffnen eines neuen Tabs blockiert."
    })
  });

  const normalize = value => {
    const base = String(value || "").toLowerCase().split(/[-_]/)[0];
    return base === "de" || base === "en" ? base : null;
  };
  const browserLocale = () => {
    for (const value of (navigator.languages || [navigator.language])) {
      const result = normalize(value);
      if (result) return result;
    }
    return "en";
  };
  let locale = browserLocale();
  try { locale = normalize(window.localStorage.getItem(STORAGE_KEY)) || locale; }
  catch (_error) { /* Storage may be unavailable in private mode. */ }

  function t(key, values = {}) {
    const message = messages[locale][key] ?? messages.en[key];
    if (message === undefined) return key;
    return message.replace(/\{([a-zA-Z][a-zA-Z0-9_]*)\}/g, (match, name) =>
      Object.hasOwn(values, name) ? String(values[name]) : match
    );
  }

  function apply() {
    document.documentElement.lang = locale;
    for (const node of document.querySelectorAll("[data-i18n]")) {
      node.textContent = t(node.dataset.i18n);
    }
    for (const node of document.querySelectorAll("[data-i18n-placeholder]")) {
      node.setAttribute("placeholder", t(node.dataset.i18nPlaceholder));
    }
    for (const node of document.querySelectorAll("[data-i18n-aria-label]")) {
      node.setAttribute("aria-label", t(node.dataset.i18nAriaLabel));
    }
    const select = document.getElementById("language-select");
    if (select) select.value = locale;
  }

  function setLocale(next, persist = true) {
    const chosen = normalize(next);
    if (!chosen) return;
    const changed = chosen !== locale;
    locale = chosen;
    if (persist) {
      try { window.localStorage.setItem(STORAGE_KEY, locale); }
      catch (_error) { /* Keep the selected language for this page. */ }
    }
    apply();
    if (changed) window.dispatchEvent(new CustomEvent("cli-agent-language-change", {detail: {locale}}));
  }

  window.cliAgentI18n = Object.freeze({ t, apply, setLocale, getLocale: () => locale });
  apply();
  document.getElementById("language-select")?.addEventListener("change", event => {
    setLocale(event.target.value);
  });
  window.addEventListener("storage", event => {
    if (event.key === STORAGE_KEY) setLocale(normalize(event.newValue) || browserLocale(), false);
  });
})();
