
(() => {
  "use strict";

  const chat = document.getElementById("chat");
  const form = document.getElementById("composer");
  const prompt = document.getElementById("prompt");
  const send = document.getElementById("send");
  const stopRun = document.getElementById("stop-run");
  const quit = document.getElementById("quit");
  // The API-backed UI supports multiple sessions; this only closes this tab's agent.
  quit.textContent = "Session beenden";
  const newSessionButton = document.getElementById("new-session");
  const listSessionsButton = document.getElementById("list-sessions");
  const sessionsDialog = document.getElementById("sessions-dialog");
  const sessionsList = document.getElementById("sessions-list");
  const closeSessionsButton = document.getElementById("close-sessions");
  const refreshSessionsButton = document.getElementById("refresh-sessions");
  const resetHistory = document.getElementById("reset-history");
  const meta = document.getElementById("session-meta");
  const showWorkingContext = document.getElementById("show-working-context");
  const workingContextDialog = document.getElementById("working-context-dialog");
  const workingMessagesJson = document.getElementById("working-messages-json");
  const contextRunSelect = document.getElementById("context-run-select");
  const cleanupProcessingResult = document.getElementById("cleanup-processing-result");
  let currentContextPayload = null;
  function renderSelectedWorkingMessages() {
    if (!currentContextPayload) return;
    const selected = contextRunSelect.value;
    const subRun = (currentContextPayload.sub_runs || []).find(item => item.id === selected);
    const cleanupRun = (currentContextPayload.cleanup_runs || []).find(item => item.id === selected);
    const run = cleanupRun || subRun;
    workingMessagesJson.textContent = JSON.stringify(run ? run.messages : currentContextPayload.messages, null, 2);
    cleanupProcessingResult.classList.toggle("hidden", !cleanupRun);
    cleanupProcessingResult.classList.toggle("error", Boolean(cleanupRun &&
      ["invalid", "failed"].includes(cleanupRun.processing_result?.status)));
    if (!cleanupRun) {
      cleanupProcessingResult.textContent = "";
      return;
    }
    const result = cleanupRun.processing_result || {};
    const labels = {
      running: "Läuft", applied: "Angewendet", unchanged: "Keine Änderung",
      invalid: "Ungültige Antwort", failed: "Fehlgeschlagen"
    };
    const bytesBefore = Number(result.bytes_before ?? 0);
    const bytesAfter = Number(result.bytes_after ?? 0);
    const lines = [
      "Verarbeitung: " + (labels[result.status] || String(result.status || "Unbekannt")),
      "Evidences: " + (result.evidences_before ?? "?") + " → " + (result.evidences_after ?? "?"),
      "Größe: " + bytesBefore + " → " + bytesAfter + " Bytes",
      "Entfernt: " + (result.removed ?? 0) + " · Aktualisiert: " +
        (result.updated ?? 0) + " · Zusammengeführt: " + (result.merged ?? 0)
    ];
    if (result.error) lines.push("Fehler: " + String(result.error));
    cleanupProcessingResult.textContent = lines.join("\n");
  }
  contextRunSelect.addEventListener("change", renderSelectedWorkingMessages);
  const workingToolsList = document.getElementById("working-tools-list");
  const workingContextsList = document.getElementById("working-contexts-list");
  const okfState = document.getElementById("okf-state");
  const refreshWorkingContext = document.getElementById("refresh-working-context");
  const closeWorkingContext = document.getElementById("close-working-context");
  const contextTabMessages = document.getElementById("context-tab-messages");
  const contextTabTools = document.getElementById("context-tab-tools");
  const contextTabContexts = document.getElementById("context-tab-contexts");
  const contextPanelMessages = document.getElementById("context-panel-messages");
  const contextPanelTools = document.getElementById("context-panel-tools");
  const contextPanelContexts = document.getElementById("context-panel-contexts");
  const commandButtons = Array.from(
    document.querySelectorAll("button[data-command]")
  );
  const cancelCommand = document.getElementById("cancel-command");
  const approval = document.getElementById("approval");
  const approvalTool = document.getElementById("approval-tool");
  const approvalArguments = document.getElementById("approval-arguments");
  const approvalDiffSection = document.getElementById("approval-diff-section");
  const approvalDiff = document.getElementById("approval-diff");
  const approvalWarning = document.getElementById("approval-warning");

  let socket = null;
  let pendingApprovalId = null;
  let pendingApprovalToolCallId = null;
  let approvalSubmitting = false;
  let pendingCommand = null;
  let busy = false;
  let activeToolGroup = null;

  const COMMANDS = {
    tokens: {
      argument: false,
      display: "tokens"
    },
    clear_web_context: {
      argument: false,
      display: "clear_web_context"
    },
    add_web_context: {
      argument: true,
      display: "add_web_context",
      question: "Welche URL soll als Web-Kontext hinzugefügt werden?",
      placeholder: "https://…"
    },
    enable: {
      argument: true,
      display: "enable",
      question: "Welcher MCP-Server soll aktiviert werden?",
      placeholder: "Name des MCP-Servers"
    },
    disable: {
      argument: true,
      display: "disable",
      question: "Welcher MCP-Server soll deaktiviert werden?",
      placeholder: "Name des MCP-Servers"
    }
  };

  function addMessage(kind, content, renderedHtml = null) {
    const element = document.createElement("div");
    element.className = "message " + kind;
    if (kind === "assistant" && typeof renderedHtml === "string") {
      element.innerHTML = renderedHtml;
    } else {
      element.textContent = content;
    }
    chat.appendChild(element);
    element.scrollIntoView({ block: "end", behavior: "smooth" });
  }

  function toolGroup() {
    if (activeToolGroup) return activeToolGroup;
    const details = document.createElement("details");
    details.className = "tool-call-group";
    details.open = true;
    const summary = document.createElement("summary");
    summary.textContent = "Tool-Aufrufe (0)";
    const list = document.createElement("div");
    list.className = "tool-call-list";
    details.append(summary, list);
    chat.appendChild(details);
    activeToolGroup = {summary, list, calls: new Map()};
    return activeToolGroup;
  }

  function toolStarted(event) {
    const id = String(event.toolCallId || "");
    if (!id) return;
    const group = toolGroup();
    // Approval resumes and SSE reconnects may replay an already shown ID.
    if (group.calls.has(id)) return;
    const row = document.createElement("div");
    row.className = "tool-call-row";
    const name = document.createElement("code");
    name.className = "tool-call-name";
    name.textContent = String(event.toolCallName || "Tool");
    const status = document.createElement("span");
    status.className = "tool-call-status";
    status.dataset.state = "running";
    status.textContent = "Läuft …";
    row.append(name, status);
    group.list.appendChild(row);
    group.calls.set(id, status);
    group.summary.textContent = "Tool-Aufrufe (" + group.calls.size + ")";
  }

  function setToolStatus(id, state) {
    const status = activeToolGroup?.calls.get(String(id || ""));
    if (!status) return;
    const labels = {
      running: "Läuft …", success: "Erfolgreich", error: "Fehler", denied: "Abgelehnt",
      approval: "Freigabe nötig", finished: "Beendet"
    };
    const normalized = Object.hasOwn(labels, state) ? state : "finished";
    status.dataset.state = normalized;
    status.textContent = labels[normalized];
  }

  function renderPublishedDocuments(event) {
    // A publication result is an AG-UI ContentPart array, not a status
    // string. Render only same-origin, server-issued artifact endpoints.
    if (!Array.isArray(event.content)) return false;
    const status = activeToolGroup?.calls.get(String(event.toolCallId || ""));
    const row = status?.parentElement;
    if (!row) return false;
    let published = false;
    for (const part of event.content) {
      if (part?.type !== "document" || part.source?.type !== "url") continue;
      const url = part.source.value;
      const filename = part.metadata?.filename;
      if (typeof url !== "string" ||
          !/^\/threads\/~[A-Za-z0-9_-]+\/artifacts\/[a-f0-9]{32}$/.test(url) ||
          typeof filename !== "string" ||
          !/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(filename)) continue;
      published = true;
      // Reconnected event streams can replay the result: show one button.
      if (status.dataset.downloadUrl === url) continue;
      status.dataset.downloadUrl = url;
      const button = document.createElement("button");
      button.type = "button";
      button.className = "secondary artifact-download";
      button.textContent = "Herunterladen: " + filename;
      button.addEventListener("click", async () => {
        button.disabled = true;
        try {
          // The normal API helper supplies the browser bearer token and the
          // current tab lease; direct links cannot access this endpoint.
          const response = await api(url);
          const blob = await response.blob();
          const objectUrl = URL.createObjectURL(blob);
          try {
            const link = document.createElement("a");
            link.href = objectUrl;
            link.download = filename;
            document.body.appendChild(link);
            link.click();
            link.remove();
          } finally {
            // Revoking immediately can cancel a browser's pending download.
            setTimeout(() => URL.revokeObjectURL(objectUrl), 60000);
          }
        } catch (error) {
          addMessage("error", "Download fehlgeschlagen: " + String(error?.message || error));
        } finally {
          button.disabled = false;
        }
      });
      row.appendChild(button);
    }
    if (published) setToolStatus(event.toolCallId, "success");
    return published;
  }

  function finishPendingTools(state) {
    if (!activeToolGroup) return;
    for (const [id, status] of activeToolGroup.calls) {
      if (status.dataset.state === "running") setToolStatus(id, state);
    }
  }

  function setBusy(value) {
    busy = Boolean(value);
    const disconnected = !sessionId;
    prompt.disabled = busy;
    send.disabled = busy || disconnected;
    stopRun.hidden = !busy || !runId;
    stopRun.disabled = disconnected || !runId;
    showWorkingContext.disabled = disconnected;
    resetHistory.disabled = busy || disconnected;
    quit.disabled = busy || disconnected;

    for (const button of commandButtons) {
      button.disabled = busy || disconnected;
    }
    for (const button of document.querySelectorAll("button[data-runtime-toggle]")) {
      const permanentlyDisabled =
        button.dataset.permanentlyDisabled === "true";
      button.disabled = busy || disconnected || permanentlyDisabled;
    }
    cancelCommand.disabled = busy || disconnected;
    if (!busy) {
      prompt.focus();
    }
  }

  function resetPendingCommand() {
    pendingCommand = null;
    prompt.placeholder = "Nachricht an cli-agent";
    cancelCommand.classList.add("hidden");
  }

  function sendCommand(name, argument = null) {
    if (busy || !sessionId) {
      return;
    }
    socket.send(JSON.stringify({
      type: "command",
      command: name,
      argument
    }));
    setBusy(true);
  }

  function beginCommand(name) {
    const spec = COMMANDS[name];
    if (!spec) {
      return;
    }
    if (!spec.argument) {
      resetPendingCommand();
      addMessage("user", spec.display);
      sendCommand(name);
      return;
    }
    pendingCommand = name;
    prompt.placeholder = spec.placeholder;
    cancelCommand.classList.remove("hidden");
    addMessage("system", spec.question);
    prompt.focus();
  }

  function setContextTab(tab) {
    const showMessages = tab === "messages";
    const showTools = tab === "tools";
    const showContexts = tab === "contexts";
    contextTabMessages.classList.toggle("active", showMessages);
    contextTabTools.classList.toggle("active", showTools);
    contextTabContexts.classList.toggle("active", showContexts);
    contextTabMessages.setAttribute("aria-selected", String(showMessages));
    contextTabTools.setAttribute("aria-selected", String(showTools));
    contextTabContexts.setAttribute("aria-selected", String(showContexts));
    contextPanelMessages.classList.toggle("hidden", !showMessages);
    contextPanelTools.classList.toggle("hidden", !showTools);
    contextPanelContexts.classList.toggle("hidden", !showContexts);
  }

  function stateToggleButton(label, enabled, onClick, disabled = false) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = enabled ? "secondary" : "";
    button.textContent = enabled ? "Disable" : "Enable";
    button.title = label;
    button.dataset.runtimeToggle = "true";
    button.dataset.permanentlyDisabled = String(Boolean(disabled));
    const disconnected = !sessionId;
    button.disabled = Boolean(disabled) || busy || disconnected;
    button.addEventListener("click", onClick);
    return button;
  }

  function renderTools(tools) {
    workingToolsList.replaceChildren();
    if (!tools.length) {
      const empty = document.createElement("div");
      empty.className = "meta";
      empty.textContent = "Keine MCP-Tools verfügbar.";
      workingToolsList.appendChild(empty);
      return;
    }
    for (const tool of tools) {
      const row = document.createElement("div");
      row.className = "state-row";
      const name = document.createElement("div");
      name.className = "state-name";
      name.textContent = String(tool.name || "");
      const description = document.createElement("div");
      description.className = "state-description";
      description.textContent = String(tool.description || "");
      const serverEnabled = tool.server_enabled !== false;
      const enabled = Boolean(tool.enabled);
      const button = stateToggleButton(
        name.textContent,
        enabled,
        () => socket.send(JSON.stringify({
          type: "tool_toggle",
          name: tool.name,
          enabled: !enabled
        })),
        !serverEnabled
      );
      if (!serverEnabled) {
        button.title = "MCP-Server ist deaktiviert.";
      }
      row.append(name, description, button);
      workingToolsList.appendChild(row);
    }
  }

  function renderContexts(contexts, okf) {
    workingContextsList.replaceChildren();
    okfState.replaceChildren();

    if (okf && okf.configured) {
      const row = document.createElement("div");
      row.className = "state-row";
      const name = document.createElement("div");
      name.className = "state-name";
      name.textContent = "OKF Knowledge";
      const description = document.createElement("div");
      description.className = "state-description";
      description.textContent = okf.available
        ? "Konfigurierter Knowledge-Lauf vor der Hauptanfrage."
        : "Konfiguriert, aber aktuell nicht verfügbar.";
      const enabled = Boolean(okf.enabled);
      const button = stateToggleButton(
        "OKF Knowledge",
        enabled,
        () => socket.send(JSON.stringify({
          type: "okf_toggle",
          enabled: !enabled
        }))
      );
      row.append(name, description, button);
      okfState.appendChild(row);
    }

    if (!contexts.length) {
      const empty = document.createElement("div");
      empty.className = "meta";
      empty.textContent = "Keine Datei- oder Web-Kontexte vorhanden.";
      workingContextsList.appendChild(empty);
      return;
    }

    for (const context of contexts) {
      const row = document.createElement("div");
      row.className = "state-row";
      const heading = document.createElement("div");
      const name = document.createElement("div");
      name.className = "state-name";
      name.textContent = String(context.label || context.source || "");
      const source = document.createElement("div");
      source.className = "state-source";
      source.textContent = (context.kind === "file" ? "Datei: " : "Web: ") +
        String(context.source || "");
      heading.append(name, source);

      const body = document.createElement("div");
      body.className = "state-description";
      const content = document.createElement("div");
      content.className = "state-content";
      content.textContent = String(context.content || "");
      body.appendChild(content);

      const enabled = Boolean(context.enabled);
      const button = stateToggleButton(
        name.textContent,
        enabled,
        () => socket.send(JSON.stringify({
          type: "context_toggle",
          id: context.id,
          enabled: !enabled
        }))
      );
      row.append(heading, body, button);
      workingContextsList.appendChild(row);
    }
  }

  function requestWorkingContext() {
    if (!sessionId) {
      const message = "Web-UI-Verbindung ist nicht aktiv.";
      workingMessagesJson.textContent = message;
      workingToolsList.textContent = message;
      workingContextsList.textContent = message;
      return;
    }
    socket.send(JSON.stringify({ type: "working_context" }));
  }

  function tokenFromFragment() {
    const params = new URLSearchParams(window.location.hash.slice(1));
    const value = params.get("token");
    try {
      if (value) window.sessionStorage.setItem("cli-agent-browser-token", value);
      return value || window.sessionStorage.getItem("cli-agent-browser-token");
    } catch (_error) { return value; }
  }

  const entryTicket = new URLSearchParams(window.location.hash.slice(1)).get("ticket");
  const token = tokenFromFragment();
  if (!token) {
    meta.textContent = "Ungültiger Start-Link: Session-Token fehlt.";
    prompt.disabled = true;
    send.disabled = true;
    return;
  }


  // In-process API transport; no WebSocket or direct agent calls.
  // Token arrives only in the URL fragment and is immediately removed.
  window.history.replaceState(null, "", window.location.pathname + window.location.search);
  let reconnectAttempts = 0;
  let sessionId = null;
  let leaseToken = null; // Page-instance secret, deliberately NOT in sessionStorage.
  let runId = null;
  let aguiMessages = [];
  let streamController = null;
  const socketBridge = {
    readyState: WebSocket.OPEN,
    send(data) {
      let msg;
      try { msg = JSON.parse(data); }
      catch (_error) { return; }
      void dispatchAction(msg).catch(showApiError);
    }
  };
  socket = socketBridge;

  async function api(path, method = "GET", body = undefined, controller = null) {
    const headers = {
      Authorization: "Bearer " + token,
      "X-Cli-Agent-Browser": "1"
    };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (path === "/ag-ui") headers["Accept"] = "text/event-stream";
    if (leaseToken) headers["X-Cli-Agent-Lease"] = leaseToken;
    // Browsers set Origin themselves for POST/PATCH; it is a forbidden header.
    const response = await fetch(path, {
      method, headers, body: body === undefined ? undefined : JSON.stringify(body),
      cache: "no-store", credentials: "omit", signal: controller?.signal
    });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.error || "HTTP " + response.status);
    }
    return response;
  }

  function emit(payload) {
    processServerMessage({ data: JSON.stringify(payload) });
  }

  function showApiError(error) {
    addMessage("error", String(error?.message || error));
    setBusy(false);
  }

  async function updateContext() {
    const resp = await api("/sessions/" + sessionId + "/working-context");
    const body = await resp.json();
    emit({ type: "working_context", ...body });
  }

  function dismissApproval() {
    pendingApprovalId = null;
    pendingApprovalToolCallId = null;
    if (approval.open) approval.close();
  }

  async function renderAnswer(text) {
    // Markdown rendering is optional: a large answer (> the JSON/render
    // request limit), a renderer failure, or a transient HTTP error must not
    // hide a successfully completed agent response. The caller renders
    // a null result using textContent, never unsanitized innerHTML.
    try {
      const rendered = await (await api("/ui/render", "POST", {text})).json();
      return typeof rendered.html === "string" ? rendered.html : null;
    } catch (_error) {
      return null;
    }
  }

  async function watchRun(activeRun, response = null) {
    // Normal runs use only AG-UI's canonical POST/SSE response. The
    // historical GET stream is used solely for reconnecting a pre-existing
    // in-flight run after a browser reload (AG-UI has no SSE resumption).
    const res = response || await api(
      "/ui/reconnect/" + sessionId + "/" + activeRun
    );
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let answer = "";
    let finished = false;
    let interrupted = false;
    try {
      while (!finished) {
        const next = await reader.read();
        if (next.done) break;
        buffer += decoder.decode(next.value, {stream: true});
        let pos;
        while ((pos = buffer.indexOf("\n\n")) !== -1) {
          const block = buffer.slice(0, pos);
          buffer = buffer.slice(pos + 2);
          const data = block.split("\n").filter(line => line.startsWith("data:"))
            .map(line => line.slice(5).trimStart()).join("\n");
          if (!data) continue;
          const event = JSON.parse(data);
          if (event.type === "TEXT_MESSAGE_CONTENT") answer += String(event.delta || "");
          else if (event.type === "TOOL_CALL_START") toolStarted(event);
          else if (event.type === "TOOL_CALL_RESULT") {
            // Document ContentParts need a separate authenticated download UI;
            // raw MCP results are never displayed as text.
            if (Array.isArray(event.content)) {
              if (!renderPublishedDocuments(event)) setToolStatus(event.toolCallId, "finished");
            } else {
              setToolStatus(event.toolCallId, String(event.content || "").toLowerCase());
            }
          } else if (event.type === "CUSTOM") {
            if (event.name === "approval_required") emit({type: "approval_required", ...event.value});
            if (event.name === "mcp_user_message") emit({type: "mcp_user_message", ...event.value});
            if (event.name === "output_error") emit({type: "error", content: event.value?.message});
          } else if (event.type === "RUN_ERROR") {
            dismissApproval();
            finishPendingTools("error");
            emit({type: "error", content: String(event.message || "Agentenlauf fehlgeschlagen.")});
            finished = true;
          } else if (event.type === "RUN_FINISHED") {
            finished = true;
            if (event.outcome?.type === "interrupt") {
              interrupted = true;
              const item = event.outcome.interrupts?.[0];
              if (item) {
                setToolStatus(item.toolCallId, "approval");
                const extra = item.metadata?.["cli-agent"] || {};
                emit({
                  type: "approval_required",
                  id: item.id,
                  tool: item.message || "Tool-Freigabe",
                  ...extra,
                  toolCallId: item.toolCallId
                });
              }
            } else {
              dismissApproval();
              finishPendingTools("finished");
            }
          }
        }
      }
      if (answer) {
        aguiMessages.push({id: crypto.randomUUID(), role: "assistant", content: answer});
        emit({type: "answer", content: answer, html: await renderAnswer(answer)});
      }
      if (!finished) throw new Error("Agenten-Eventstream vorzeitig beendet.");
    } catch (error) {
      finishPendingTools("error");
      throw error;
    } finally {
      if (!interrupted) dismissApproval();
      reader.releaseLock();
      runId = null;
      emit({type: "busy", value: interrupted});
    }
  }

  // The server accepts at most 512 messages and 1 MiB of request JSON.
  // Keep a safety margin for envelope fields and UTF-8 string escaping.
  const MAX_SYNCED_MESSAGES = 480;
  const MAX_SYNCED_REQUEST_BYTES = 800000;
  const jsonEncoder = new TextEncoder();

  function compactAguiHistory(input) {
    const messages = input.messages;
    const utf8Bytes = value => jsonEncoder.encode(JSON.stringify(value)).length;
    // Compute the serialized request size once. Each removed array item
    // contributes its serialized UTF-8 bytes plus exactly one comma, as the
    // latest conversation turn is always retained. Avoid repeatedly encoding
    // the complete history (quadratic cost on slower Windows runners).
    let bytes = utf8Bytes(input);
    let start = 0;
    const lastUser = messages.findLastIndex(item => item.role === "user");
    // Remove *whole* prior conversation turns. Never discard the newest
    // user turn or an in-progress tool/approval exchange to make room.
    while ((messages.length - start > MAX_SYNCED_MESSAGES ||
            bytes > MAX_SYNCED_REQUEST_BYTES) &&
           messages.length - start > 1) {
      if (lastUser <= start) break;
      let next = start + 1;
      while (next < lastUser && messages[next].role !== "user") next += 1;
      for (let index = start; index < next; index += 1) {
        bytes -= utf8Bytes(messages[index]) + 1;
      }
      start = next;
    }
    if (messages.length - start > MAX_SYNCED_MESSAGES ||
        bytes > MAX_SYNCED_REQUEST_BYTES) {
      throw new Error(
        "Die aktuelle Nachricht oder Tool-Fortsetzung ist zu groß. " +
        "Bitte den Inhalt verkürzen."
      );
    }
    return messages.slice(start);
  }

  // Keep the wire transcript below the server limits of 512 messages
  // and 1 MiB JSON. The visible chat transcript is independent and stays
  // intact. Keep recent complete turns; never silently truncate a message.
  const MAX_WIRE_MESSAGES = 240;
  const MAX_WIRE_BYTES = 400 * 1024;
  function boundAguiMessages() {
    const encoder = new TextEncoder();
    let start = Math.max(0, aguiMessages.length - MAX_WIRE_MESSAGES);
    let bytes = 0;
    for (let index = aguiMessages.length - 1; index >= start; index -= 1) {
      bytes += encoder.encode(JSON.stringify(aguiMessages[index])).length + 1;
      if (bytes > MAX_WIRE_BYTES) {
        start = index + 1;
        break;
      }
    }
    // The latest user message cannot be discarded: the agent must either
    // receive the whole request or return a clear size error.
    if (start >= aguiMessages.length) {
      throw new Error("Letzte Nachricht überschreitet das AG-UI-Limit.");
    }
    aguiMessages = aguiMessages.slice(start);
    // Avoid sending an orphaned tool result or assistant reply as the
    // earliest retained conversation turn after the truncation.
    while (aguiMessages.length > 1 && aguiMessages[0].role !== "user") {
      aguiMessages.shift();
    }
    return aguiMessages;
  }

  function aguiInput(resume = undefined) {
    const result = {
      threadId: sessionId,
      runId: crypto.randomUUID(),
      protocolVersion: "1.0",
      messages: boundAguiMessages()
    };
    if (resume) result.resume = resume;
    result.messages = compactAguiHistory(result);
    return result;
  }

  async function dispatchAction(msg) {
    if (!sessionId) return;
    const base = "/sessions/" + sessionId;
    if (msg.type === "cancel_run") {
      if (!runId) return;
      stopRun.disabled = true;
      try {
        await api("/ag-ui", "POST", {action: "cancel", threadId: sessionId, runId});
      } catch (error) {
        stopRun.disabled = false;
        throw error;
      }
      return;
    }
    if (msg.type === "message") {
      if (typeof msg.content !== "string") throw new Error("Ungültiger Prompt.");
      if (["exit", "quit"].includes(msg.content.trim().toLowerCase())) {
        await deleteUnusedSession(sessionId);
        if (sessionId) setBusy(false);
        return;
      }
      aguiMessages.push({id: crypto.randomUUID(), role: "user", content: msg.content});
      let response;
      let input;
      try {
        input = aguiInput();
        response = await api("/ag-ui", "POST", input);
      } catch (error) {
        // A rejected run never started: do not retain an unprocessed user
        // message as if it were part of the authoritative conversation.
        aguiMessages.pop();
        throw error;
      }
      aguiMessages = input.messages;
      runId = input.runId;
      setBusy(true);
      await watchRun(runId, response);
      return;
    }
    if (msg.type === "command") {
      const spec = COMMANDS[msg.command];
      if (!spec || (spec.argument && typeof msg.argument !== "string") ||
          (!spec.argument && msg.argument != null && msg.argument !== "")) {
        throw new Error("Ungültiger lokaler Befehl.");
      }
      const result = await (await api("/ui/command", "POST", {
        session_id: sessionId, command: msg.command,
        argument: msg.argument == null ? null : msg.argument
      })).json();
      const answer = String(result.answer || "");
      emit({type: "answer", content: answer, html: await renderAnswer(answer)});
      emit({type: "busy", value: false});
      return;
    }
    if (msg.type === "working_context") return updateContext();
    if (msg.type === "reset_history") {
      await api(base + "/reset", "POST", {});
      aguiMessages = [];
      emit({type:"history_reset"});
      return updateContext();
    }
    if (msg.type === "tool_toggle" || msg.type === "context_toggle" || msg.type === "okf_toggle") {
      let patch;
      if (msg.type === "tool_toggle") patch = {tools: {[msg.name]: msg.enabled}};
      if (msg.type === "context_toggle") patch = {contexts: {[msg.id]: msg.enabled}};
      if (msg.type === "okf_toggle") patch = {okf: msg.enabled};
      await api(base + "/context", "PATCH", patch);
      return updateContext();
    }
    if (msg.type === "approval_response") {
      if (msg.id !== pendingApprovalId || approvalSubmitting) return;
      approvalSubmitting = true;
      const buttons = approval.querySelectorAll("button[data-decision]");
      for (const button of buttons) button.disabled = true;
      try {
        const input = aguiInput([{
          interruptId: msg.id,
          status: "resolved",
          payload: {
            approved: msg.decision === "yes" || msg.decision === "session",
            rememberForSession: msg.decision === "session"
          }
        }]);
        // An interrupted run still occupies its server-side approval slot.
        // Resume may be rejected (e.g. HTTP 429) before the server accepts it.
        // Keep the dialog and interrupt ID intact until the POST succeeds.
        const response = await api("/ag-ui", "POST", input);
        aguiMessages = input.messages;
        // Resumed interrupt IDs intentionally have no new TOOL_CALL_START event.
        // Only update the row after the server accepts the resume request.
        setToolStatus(pendingApprovalToolCallId,
          msg.decision === "yes" || msg.decision === "session" ? "running" : "denied");
        dismissApproval();
        runId = input.runId;
        setBusy(true);
        await watchRun(runId, response);
      } catch (error) {
        if (pendingApprovalId === msg.id) {
          addMessage("error", String(error?.message || error));
          setBusy(true);
          return;
        }
        throw error;
      } finally {
        approvalSubmitting = false;
        for (const button of buttons) button.disabled = false;
      }
      return;
    }
  }

  function processServerMessage(event) {
    let payload;
    try {
      payload = JSON.parse(event.data);
    } catch (_error) {
      addMessage("error", "Ungültige Antwort vom lokalen Web-UI-Server.");
      return;
    }

    if (payload.type === "session") {
      reconnectAttempts = 0;
      const servers = Array.isArray(payload.mcp_servers)
        ? payload.mcp_servers.join(", ")
        : "";
      meta.textContent =
        "Workspace: " + payload.workspace +
        " · Modell: " + payload.model +
        " · MCP: " + (servers || "(keine)");
      for (const message of payload.initial_messages || []) {
        addMessage("system", String(message));
      }
      return;
    }
    if (payload.type === "busy") {
      setBusy(payload.value);
      return;
    }
    if (payload.type === "answer") {
      addMessage(
        "assistant",
        String(payload.content || ""),
        typeof payload.html === "string" ? payload.html : null
      );
      return;
    }
    if (payload.type === "error") {
      addMessage("error", String(payload.content || "Unbekannter Fehler."));
      return;
    }
    if (payload.type === "mcp_user_message") {
      const source = String(payload.source || "").trim();
      const content = String(payload.content || "");
      addMessage(
        "mcp-user",
        source ? "MCP (" + source + ")\n" + content : content
      );
      return;
    }
    if (payload.type === "working_context") {
      const messages = Array.isArray(payload.messages) ? payload.messages : [];
      const tools = Array.isArray(payload.tools) ? payload.tools : [];
      const contexts = Array.isArray(payload.contexts) ? payload.contexts : [];
      currentContextPayload = payload;
      const previous = contextRunSelect.value;
      contextRunSelect.replaceChildren();
      contextRunSelect.add(new Option("Hauptlauf", "main"));
      for (const run of (Array.isArray(payload.sub_runs) ? payload.sub_runs : [])) {
        contextRunSelect.add(new Option("Sub-Run: " + run.task.slice(0, 75), run.id));
      }
      for (const run of (Array.isArray(payload.cleanup_runs) ? payload.cleanup_runs : [])) {
        const cause = run.trigger === "plan_changed" ? "Planänderung" : "20 Toolaufrufe";
        contextRunSelect.add(new Option("Cleanup #" + String(run.id).replace(/^cleanup-/, "") + " – " + cause, run.id));
      }
      contextRunSelect.value = [...contextRunSelect.options].some(o => o.value === previous) ? previous : "main";
      renderSelectedWorkingMessages();
      renderTools(tools);
      renderContexts(contexts, payload.okf || {});
      return;
    }
    if (payload.type === "history_reset") {
      resetPendingCommand();
      chat.replaceChildren();
      activeToolGroup = null;
      addMessage("system", "History wurde zurückgesetzt.");
      return;
    }
    if (payload.type === "approval_required") {
      pendingApprovalId = String(payload.id);
      pendingApprovalToolCallId = String(payload.toolCallId || "");
      approvalTool.textContent = String(payload.tool || "");
      approvalArguments.textContent = String(payload.arguments || "{}");
      approvalDiffSection.classList.toggle("hidden", typeof payload.diff !== "string");
      approvalDiff.replaceChildren();
      if (typeof payload.diff === "string") {
        for (const line of payload.diff.split("\n")) {
          const span = document.createElement("span");
          span.textContent = line + "\n";
          if (line.startsWith("+") && !line.startsWith("+++")) span.style.color = "#288a42";
          if (line.startsWith("-") && !line.startsWith("---")) span.style.color = "#d24747";
          approvalDiff.append(span);
        }
      }
      approvalWarning.classList.toggle("hidden", !payload.diff_error);
      approvalWarning.textContent = String(payload.diff_error || "");
      for (const button of approval.querySelectorAll('button[data-decision="yes"], button[data-decision="session"]')) {
        button.disabled = !!payload.diff_blocked;
      }
      approval.showModal();
      return;
    }
  }

  function sessionLink(ticket) {
    return window.location.origin + "/#token=" +
      encodeURIComponent(token) + "&ticket=" + encodeURIComponent(ticket);
  }

  function openTicketInTab(ticket, tab) {
    tab.opener = null;
    tab.location.replace(sessionLink(ticket));
  }

  async function createNewTab() {
    // Open synchronously in the click handler; otherwise popup blockers
    // might reject it after waiting for the API response.
    const tab = window.open("about:blank", "_blank");
    if (!tab) {
      addMessage("error", "Browser hat das Öffnen eines neuen Tabs blockiert.");
      return;
    }
    try {
      const response = await (await api("/ui/sessions", "POST", {})).json();
      openTicketInTab(response.ticket, tab);
      if (sessionsDialog.open) await refreshSessions();
    } catch (error) {
      tab.close();
      showApiError(error);
    }
  }

  async function openExistingSession(sid) {
    const tab = window.open("about:blank", "_blank");
    if (!tab) {
      addMessage("error", "Browser hat das Öffnen eines neuen Tabs blockiert.");
      return;
    }
    try {
      const response = await (
        await api("/ui/sessions/" + encodeURIComponent(sid) + "/ticket", "POST", {})
      ).json();
      openTicketInTab(response.ticket, tab);
      await refreshSessions();
    } catch (error) {
      tab.close();
      showApiError(error);
    }
  }

  async function deleteUnusedSession(sid) {
    if (!window.confirm("Diese Session dauerhaft beenden? Andere Sessions bleiben geöffnet.")) return;
    await api("/ui/sessions/" + encodeURIComponent(sid), "DELETE");
    if (sid === sessionId) {
      // Only the current agent worker was removed, not the API server.
      // Do not clear the browser access token; it is also used for other tabs.
      sessionId = null;
      leaseToken = null;
      runId = null;
      aguiMessages = [];
      dismissApproval();
      resetPendingCommand();
      if (heartbeat !== null) {
        window.clearInterval(heartbeat);
        heartbeat = null;
      }
      try { window.sessionStorage.removeItem("cli-agent-session-id"); }
      catch (_error) {}
      chat.replaceChildren();
      meta.textContent = "Session beendet. Andere Sessions laufen weiter. Neue Session in neuem Tab öffnen.";
      setBusy(true);
    }
    await refreshSessions();
  }

  async function refreshSessions() {
    const result = await (await api("/ui/sessions")).json();
    sessionsList.replaceChildren();
    for (const item of result.sessions) {
      const row = document.createElement("div");
      row.className = "state-row";
      const name = document.createElement("div");
      name.className = "state-name";
      const current = item.session_id === sessionId;
      const executing = item.busy && !item.approval_pending;
      name.textContent = item.session_id.slice(0, 12) +
        (current ? " · aktueller Tab" : item.occupied ? " · geöffnet" : " · frei");
      const actions = document.createElement("div");
      const open = document.createElement("button");
      open.type = "button";
      open.className = "secondary";
      open.textContent = "Öffnen";
      open.disabled = item.occupied || executing;
      open.addEventListener("click", () => { void openExistingSession(item.session_id); });
      const close = document.createElement("button");
      close.type = "button";
      close.className = "secondary";
      close.textContent = "Beenden";
      close.disabled = (item.occupied && !current) || executing;
      close.addEventListener("click", () => {
        void deleteUnusedSession(item.session_id).catch(showApiError);
      });
      actions.append(open, close);
      row.append(name, actions);
      sessionsList.appendChild(row);
    }
  }

  let heartbeat = null;
  function startHeartbeat() {
    if (heartbeat !== null) window.clearInterval(heartbeat);
    heartbeat = window.setInterval(async () => {
      if (!sessionId || !leaseToken) return;
      try {
        await api("/ui/heartbeat", "POST", {session_id: sessionId});
      } catch (_error) {
        leaseToken = null;
        sessionId = null;
        setBusy(true);
        meta.textContent = "Session-Bindung verloren. Tab erneut öffnen.";
      }
    }, 8000);
  }

  window.addEventListener("pagehide", () => {
    if (!leaseToken || !sessionId) return;
    void fetch("/ui/release", {
      method: "POST",
      headers: {
        "Authorization": "Bearer " + token,
        "X-Cli-Agent-Browser": "1",
        "X-Cli-Agent-Lease": leaseToken,
        "Content-Type": "application/json"
      },
      body: JSON.stringify({session_id: sessionId}),
      credentials: "omit",
      cache: "no-store",
      keepalive: true
    }).catch(() => {});
  });

  async function bootstrap() {
    const ticket = entryTicket;
    let previous = null;
    try { previous = window.sessionStorage.getItem("cli-agent-session-id"); }
    catch (_error) {}
    const payload = ticket ? {ticket} : (previous ? {session_id: previous} : {});
    window.history.replaceState(null, "", window.location.pathname + window.location.search);
    try {
      let response;
      // pagehide release on reload is asynchronous. Allow a short handoff
      // interval, but NEVER steal an active lease from another tab.
      for (let attempt = 0; attempt < 6; attempt += 1) {
        try {
          response = await (await api("/ui/bootstrap", "POST", payload)).json();
          break;
        } catch (error) {
          if (ticket || !String(error.message).includes("bereits in einem anderen Tab")
                     || attempt === 5) throw error;
          await new Promise(resolve => window.setTimeout(resolve, 300));
        }
      }
      sessionId = response.session_id;
      leaseToken = response.lease;
      aguiMessages = Array.isArray(response.agui_messages)
        ? response.agui_messages : [];
      try { window.sessionStorage.setItem("cli-agent-session-id", sessionId); }
      catch (_error) {}
      chat.replaceChildren();
      activeToolGroup = null;
      emit({type: "session", ...response});
      for (const message of response.history || []) {
        if (message.role === "assistant") {
          // Render only server-sanitized Markdown; never inject model HTML.
          const content = String(message.content || "");
          addMessage("assistant", content, await renderAnswer(content));
        } else if (message.role === "user") {
          addMessage("user", String(message.content || ""));
        }
      }
      if (response.pending_interrupt) {
        const item = response.pending_interrupt;
        toolStarted({toolCallId: item.toolCallId, toolCallName: item.message});
        setToolStatus(item.toolCallId, "approval");
        emit({
          type: "approval_required", id: item.id,
          tool: item.message || "Tool-Freigabe",
          ...(item.metadata?.["cli-agent"] || {}),
          toolCallId: item.toolCallId
        });
        emit({type: "busy", value: true});
      } else if (response.active_run) {
        runId = response.active_run;
        emit({type: "busy", value: true});
        void watchRun(runId).catch(showApiError);
      } else {
        emit({type: "busy", value: false});
      }
      startHeartbeat();
    } catch (error) {
      meta.textContent = "Session konnte nicht geöffnet werden.";
      showApiError(error);
    }
  }
  setBusy(true);
  void bootstrap();

  newSessionButton.addEventListener("click", () => { void createNewTab(); });
  listSessionsButton.addEventListener("click", () => {
    sessionsDialog.showModal();
    void refreshSessions().catch(showApiError);
  });
  closeSessionsButton.addEventListener("click", () => sessionsDialog.close());
  refreshSessionsButton.addEventListener("click", () => {
    void refreshSessions().catch(showApiError);
  });

  showWorkingContext.addEventListener("click", () => {
    workingMessagesJson.textContent = "Wird geladen …";
    workingToolsList.textContent = "Wird geladen …";
    workingContextsList.textContent = "Wird geladen …";
    okfState.replaceChildren();
    setContextTab("messages");
    if (!workingContextDialog.open) {
      workingContextDialog.showModal();
    }
    requestWorkingContext();
  });

  refreshWorkingContext.addEventListener("click", () => {
    workingMessagesJson.textContent = "Wird geladen …";
    workingToolsList.textContent = "Wird geladen …";
    workingContextsList.textContent = "Wird geladen …";
    okfState.replaceChildren();
    requestWorkingContext();
  });

  closeWorkingContext.addEventListener("click", () => {
    workingContextDialog.close();
  });

  contextTabMessages.addEventListener("click", () => setContextTab("messages"));
  contextTabTools.addEventListener("click", () => setContextTab("tools"));
  contextTabContexts.addEventListener("click", () => setContextTab("contexts"));

  resetHistory.addEventListener("click", () => {
    if (busy || !sessionId) {
      return;
    }
    if (!window.confirm("Conversation-History wirklich zurücksetzen?")) {
      return;
    }
    socket.send(JSON.stringify({ type: "reset_history" }));
  });

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (busy || !sessionId) {
      return;
    }
    const content = prompt.value.trim();
    if (!content) {
      return;
    }
    // A rejected session-exit confirmation is not a run. Do not mark the
    // composer busy or append a fake conversation turn before confirmation.
    if (!pendingCommand && ["exit", "quit"].includes(content.toLowerCase())) {
      void deleteUnusedSession(sessionId).catch(showApiError);
      return;
    }
    // Group subsequent tool events with this user turn, across approvals.
    activeToolGroup = null;
    addMessage("user", content);
    prompt.value = "";
    if (pendingCommand) {
      const command = pendingCommand;
      resetPendingCommand();
      sendCommand(command, content);
      return;
    }
    socket.send(JSON.stringify({ type: "message", content }));
    setBusy(true);
  });

  prompt.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      form.requestSubmit();
    }
  });

  for (const button of commandButtons) {
    button.addEventListener("click", () => {
      beginCommand(button.dataset.command);
    });
  }

  stopRun.addEventListener("click", () => {
    void dispatchAction({type: "cancel_run"}).catch(showApiError);
  });

  cancelCommand.addEventListener("click", () => {
    if (pendingCommand) {
      addMessage("system", "Befehl abgebrochen.");
    }
    resetPendingCommand();
    prompt.focus();
  });

  approval.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-decision]");
    if (!button || !pendingApprovalId || !sessionId || approvalSubmitting) {
      return;
    }
    socket.send(JSON.stringify({
      type: "approval_response",
      id: pendingApprovalId,
      decision: button.dataset.decision
    }));
  });

  approval.addEventListener("cancel", (event) => {
    event.preventDefault();
    if (pendingApprovalId && sessionId && !approvalSubmitting) {
      socket.send(JSON.stringify({
        type: "approval_response",
        id: pendingApprovalId,
        decision: "no"
      }));
    }
  });

  quit.addEventListener("click", () => {
    if (sessionId && !busy) {
      void deleteUnusedSession(sessionId).catch(showApiError);
    }
  });
})();
