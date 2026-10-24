# API-backed variant of the existing Web-UI JavaScript.
# The browser talks only to the Starlette API endpoints and never to agent.ask().
API_APP_JS = """
(() => {
  "use strict";

  const chat = document.getElementById("chat");
  const form = document.getElementById("composer");
  const prompt = document.getElementById("prompt");
  const send = document.getElementById("send");
  const quit = document.getElementById("quit");
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
  let pendingCommand = null;
  let busy = false;

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

  function setBusy(value) {
    busy = Boolean(value);
    const disconnected = !sessionId;
    prompt.disabled = busy;
    send.disabled = busy || disconnected;
    showWorkingContext.disabled = disconnected;
    resetHistory.disabled = busy || disconnected;

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
      "/sessions/" + sessionId + "/runs/" + activeRun + "/events"
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
        while ((pos = buffer.indexOf("\\n\\n")) !== -1) {
          const block = buffer.slice(0, pos);
          buffer = buffer.slice(pos + 2);
          const data = block.split("\\n").filter(line => line.startsWith("data:"))
            .map(line => line.slice(5).trimStart()).join("\\n");
          if (!data) continue;
          const event = JSON.parse(data);
          if (event.type === "TEXT_MESSAGE_CONTENT") answer += String(event.delta || "");
          else if (event.type === "CUSTOM") {
            if (event.name === "approval_required") emit({type: "approval_required", ...event.value});
            if (event.name === "mcp_user_message") emit({type: "mcp_user_message", ...event.value});
            if (event.name === "output_error") emit({type: "error", content: event.value?.message});
          } else if (event.type === "RUN_ERROR") {
            dismissApproval();
            emit({type: "error", content: String(event.message || "Agentenlauf fehlgeschlagen.")});
            finished = true;
          } else if (event.type === "RUN_FINISHED") {
            finished = true;
            if (event.outcome?.type === "interrupt") {
              interrupted = true;
              const item = event.outcome.interrupts?.[0];
              if (item) {
                const extra = item.metadata?.["cli-agent"] || {};
                emit({
                  type: "approval_required",
                  id: item.id,
                  tool: item.message || "Tool-Freigabe",
                  ...extra
                });
              }
            } else {
              dismissApproval();
            }
          }
        }
      }
      if (answer) {
        aguiMessages.push({id: crypto.randomUUID(), role: "assistant", content: answer});
        emit({type: "answer", content: answer, html: await renderAnswer(answer)});
      }
      if (!finished) throw new Error("Agenten-Eventstream vorzeitig beendet.");
    } finally {
      if (!interrupted) dismissApproval();
      reader.releaseLock();
      runId = null;
      emit({type: "busy", value: interrupted});
    }
  }

  function aguiInput(resume = undefined) {
    const result = {
      threadId: sessionId,
      runId: crypto.randomUUID(),
      protocolVersion: "1.0",
      messages: aguiMessages
    };
    if (resume) result.resume = resume;
    return result;
  }

  async function dispatchAction(msg) {
    if (!sessionId) return;
    const base = "/sessions/" + sessionId;
    if (msg.type === "message") {
      if (typeof msg.content !== "string") throw new Error("Ungültiger Prompt.");
      if (["exit", "quit"].includes(msg.content.trim().toLowerCase())) {
        await dispatchAction({type: "quit"});
        return;
      }
      aguiMessages.push({id: crypto.randomUUID(), role: "user", content: msg.content});
      const input = aguiInput();
      const response = await api("/ag-ui", "POST", input);
      runId = input.runId;
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
      if (msg.id !== pendingApprovalId) return;
      const input = aguiInput([{
        interruptId: msg.id,
        status: "resolved",
        payload: {
          approved: msg.decision === "yes" || msg.decision === "session",
          rememberForSession: msg.decision === "session"
        }
      }]);
      const response = await api("/ag-ui", "POST", input);
      pendingApprovalId = null;
      runId = input.runId;
      await watchRun(runId, response);
      return;
    }
    if (msg.type === "quit") {
      await api("/ui/shutdown", "POST", {});
      try { window.sessionStorage.removeItem("cli-agent-browser-token"); } catch (_error) {}
      emit({type:"shutdown", content:"Web-UI wird beendet."});
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
      quit.disabled = false;
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
        source ? "MCP (" + source + ")\\n" + content : content
      );
      return;
    }
    if (payload.type === "working_context") {
      const messages = Array.isArray(payload.messages) ? payload.messages : [];
      const tools = Array.isArray(payload.tools) ? payload.tools : [];
      const contexts = Array.isArray(payload.contexts) ? payload.contexts : [];
      workingMessagesJson.textContent = JSON.stringify(messages, null, 2);
      renderTools(tools);
      renderContexts(contexts, payload.okf || {});
      return;
    }
    if (payload.type === "history_reset") {
      resetPendingCommand();
      chat.replaceChildren();
      addMessage("system", "History wurde zurückgesetzt.");
      return;
    }
    if (payload.type === "approval_required") {
      pendingApprovalId = String(payload.id);
      approvalTool.textContent = String(payload.tool || "");
      approvalArguments.textContent = String(payload.arguments || "{}");
      approvalDiffSection.classList.toggle("hidden", typeof payload.diff !== "string");
      approvalDiff.replaceChildren();
      if (typeof payload.diff === "string") {
        for (const line of payload.diff.split("\\n")) {
          const span = document.createElement("span");
          span.textContent = line + "\\n";
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
    if (payload.type === "shutdown") {
      addMessage("system", String(payload.content || "Sitzung beendet."));
      setBusy(true);
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
    if (!window.confirm("Diese Session dauerhaft beenden?")) return;
    await api("/ui/sessions/" + encodeURIComponent(sid), "DELETE");
    if (sid === sessionId) {
      sessionId = null;
      leaseToken = null;
      runId = null;
      try { window.sessionStorage.removeItem("cli-agent-session-id"); }
      catch (_error) {}
      chat.replaceChildren();
      meta.textContent = "Session beendet. Neue Session in neuem Tab öffnen.";
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
      name.textContent = item.session_id.slice(0, 12) +
        (current ? " · aktueller Tab" : item.occupied ? " · geöffnet" : " · frei");
      const actions = document.createElement("div");
      const open = document.createElement("button");
      open.type = "button";
      open.className = "secondary";
      open.textContent = "Öffnen";
      open.disabled = item.occupied || item.busy;
      open.addEventListener("click", () => { void openExistingSession(item.session_id); });
      const close = document.createElement("button");
      close.type = "button";
      close.className = "secondary";
      close.textContent = "Beenden";
      close.disabled = (item.occupied && !current) || item.busy;
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
        emit({
          type: "approval_required", id: item.id,
          tool: item.message || "Tool-Freigabe",
          ...(item.metadata?.["cli-agent"] || {})
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

  cancelCommand.addEventListener("click", () => {
    if (pendingCommand) {
      addMessage("system", "Befehl abgebrochen.");
    }
    resetPendingCommand();
    prompt.focus();
  });

  approval.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-decision]");
    if (!button || !pendingApprovalId || !sessionId) {
      return;
    }
    socket.send(JSON.stringify({
      type: "approval_response",
      id: pendingApprovalId,
      decision: button.dataset.decision
    }));
    pendingApprovalId = null;
    approval.close();
  });

  approval.addEventListener("cancel", (event) => {
    event.preventDefault();
    if (pendingApprovalId && sessionId) {
      socket.send(JSON.stringify({
        type: "approval_response",
        id: pendingApprovalId,
        decision: "no"
      }));
    }
    pendingApprovalId = null;
    approval.close();
  });

  quit.addEventListener("click", () => {
    if (sessionId) {
      socket.send(JSON.stringify({ type: "quit" }));
    }
  });
})();
"""


async def run_web_ui_via_api(
    *,
    args,
    config,
    admin_config,
    workspace,
    file_contexts,
    output_target,
    excluded_paths,
):
    """Keep cli-agent --with-web-ui a one-command localhost launch.

    Uvicorn and the session worker run in one process. All agent operations
    execute on the AgentSession worker task, including MCP enter/exit.
    """
    import asyncio
    import secrets
    import socket
    import webbrowser

    import uvicorn

    from .agent_api import API_HOST, AgentApiService, create_app
    from .cli import build_preapproval_callback
    from .config import McpServerConfig, default_config_file
    from .file_context import ContextFileCliAgent
    from .model_factory import create_model_client
    from .logging_setup import configure_logging
    from .os_operations import Workspace
    from .terminal_output import sanitize_terminal_text

    configure_logging(config.logging)
    config_file = args.config or default_config_file()
    exclusions = tuple(excluded_paths)
    preview_workspace = (
        Workspace.from_directory(
            workspace,
            McpServerConfig(name="os", config={"allow_write_files": True}),
            protected_paths=(config_file, *exclusions),
            mutation_protected_paths=tuple(
                item.source_path for item in file_contexts
            ),
        )
        if args.os_access == "write" else None
    )

    def factory(session):
        client = create_model_client(
            config.model, network=admin_config.network,
            credential_rules=admin_config.model_credentials,
        )
        agent = ContextFileCliAgent(
            workspace, client, config.mcp_servers,
            logging_config=config.logging,
            config_file=config_file,
            dump_llm_context=False,
            network=admin_config.network,
            web_providers=admin_config.web.providers,
            mcp_policy=admin_config.mcp,
            approval_callback=build_preapproval_callback(
                getattr(args, "approve_tool", ()) or (),
                fallback=session.approve,
            ),
            user_message_callback=session.broker.show_mcp_user_message,
            okf=config.okf,
            knowledge_vector=config.knowledge_vector,
            model_credential_rules=admin_config.model_credentials,
            file_contexts=tuple(file_contexts),
            response_format=getattr(args, "response_format", "text"),
            workspace_access=args.os_access,
        )
        agent.event_callback = session.on_agent_event
        session.broker.set_event_sender(session._send_user_message)
        return agent

    service = AgentApiService(
        factory,
        approval_workspace=preview_workspace,
        output_target=output_target,
        debug_errors=bool(getattr(args, 'debug', False)),
        initial_commands=tuple(
            f"add_web_context {url}"
            for url in getattr(args, "add_web_context", ()) or ()
        ),
    )

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((API_HOST, 0))
        sock.listen(128)
        sock.setblocking(False)
        port = int(sock.getsockname()[1])
        origin = f"http://{API_HOST}:{port}"
        session = await service.create()
        from .web_ui_sessions import BrowserSessionRegistry
        registry = BrowserSessionRegistry(service, session.id)
        browser_token = secrets.token_urlsafe(32)
        ui = {
            "registry": registry,
            "token": browser_token,
            "origin": origin,
            "session_id": session.id,
            "info": {
                "workspace": str(workspace),
                "model": config.model.model,
                "mcp_servers": [item.name for item in config.mcp_servers],
            },
        }
        app = create_app(service, ui=ui)
        server = uvicorn.Server(uvicorn.Config(
            app, host=API_HOST, port=port,
            log_level="warning", access_log=False,
            proxy_headers=False, server_header=False,
        ))
        ui["shutdown"] = lambda: setattr(server, "should_exit", True)
        print(sanitize_terminal_text(
            f"Web-UI: {origin}/ (nur localhost)",
            multiline=False, escape_invisible_formatting=True,
        ))
        print("Web-UI Access Token:", browser_token)
        print("Zum Beenden Strg+C im Terminal oder 'Sitzung beenden' im Browser.")
        server_task = asyncio.create_task(server.serve(sockets=[sock]))
        while not server.started and not server_task.done():
            await asyncio.sleep(0.01)
        if server_task.done():
            await server_task
            return
        await asyncio.to_thread(webbrowser.open, f"{origin}/#token={browser_token}", new=2)
        await server_task
    finally:
        await service.shutdown()
        sock.close()
