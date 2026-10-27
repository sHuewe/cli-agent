# Built-in Web UI via the Agent API

Run the embedded browser UI with the existing CLI argument:

```console
cli-agent --with-web-ui
```

No separate `cli-agent-api` process, API port, or token setup is required. The CLI
starts **one** localhost-only Uvicorn/Starlette application that serves the
browser assets and the existing Agent API on an ephemeral port. An
`AgentApiService` manages the agent session; the browser does **not** call
`agent.ask()` directly and does not use the legacy WebSocket path.

The console displays and opens a local URL with a browser token in its fragment.
The frontend removes the fragment and keeps the credential within that tab's
session storage for reloads. The browser token is separate from the API bearer
credential and can manage the local UI sessions but cannot directly control an
agent session without its separate exclusive tab lease.
Browser state-changing requests must carry the same-origin Origin header and
an explicit browser marker. Host validation and loopback-only binding remain in
place. The API mode does not write LLM context dumps.

## API communication

The frontend sends standard AG-UI 1.0 `RunAgentInput` requests to
`POST /ag-ui` and reads the streamed SSE response from that **same**
HTTP request. This includes user/assistant messages, backend tool events,
and human approval via `RUN_FINISHED` interrupt plus a subsequent new AG-UI
run with `resume` decision. There is no separate `POST /runs` / `GET /events`
handshake in the normal UI workflow.

The browser displays backend tool calls in a collapsible **Tool-Aufrufe**
section for each user turn, including tools that were already approved.
The list shows only the exposed tool name and execution status, not tool
arguments or result content. Approval interrupts and their resumed execution
share one entry. These live event details are not part of the persisted
user/assistant transcript and are therefore not restored for completed runs
after a page reload.

CLI-only commands such as `tokens`, `add_web_context`,
`clear_web_context`, `enable` and `disable` use the validated
`POST /ui/command` extension. These commands are deliberately not part
of AG-UI and are not required by generic clients.

See [Native AG-UI API](ag-ui.md) for the protocol and SignalDeck integration.

The Working Context dialog uses
`GET /sessions/{id}/working-context` and
`PATCH /sessions/{id}/context` for file/web-context, tool and OKF switches.
Other operations use `POST /sessions/{id}/reset`,
`GET /sessions/{id}/usage`, and the validated local commands already
understood by the agent. The browser renders agent Markdown via the
authenticated local `POST /ui/render` route.

CLI options, including `--context-file`, `--add-web-context`, `--output`,
`--overwrite-output`, `--approve-tool`, `--with-os-read/write`,
`--with-mcp`, `--with-data`, `--response-format` and `--debug`, are
passed into the local API-backed agent. Initial web contexts are loaded on
the agent's worker task before its session becomes ready.

## Multiple browser sessions

The built-in UI can manage up to eight independent agent sessions on the same
Starlette server. To reserve HTTP/1.1 connections for tool approvals, lease
heartbeats and UI control requests, no more than four embedded-browser runs
may be active simultaneously. The fifth concurrent run request returns HTTP
429 and can be retried once another run completes. This limit does not affect
headless API clients or prevent idle UI sessions from remaining open. Click **Neue Session in neuem Tab** to create a session and open it in
a new tab. The **Sessions** dialog lists existing sessions, allows reopening
unoccupied sessions (including those awaiting tool approval), and deleting
sessions that are not actively executing a run. An approval-interrupted
session can be reopened or explicitly closed without affecting other tabs.

The new-tab URL includes a short-lived **single-use ticket** and the browser
access token in its URL fragment. The fragment is removed on load. Copying the
cleaned URL into another tab is not a supported way to open a session. Each
claimed session receives a random server-side lease whose secret exists only
in the JavaScript context of the active page, not in sessionStorage.

Each agent session may have **at most one active tab**, enforced by the API
middleware for every session-scoped request. A second tab gets HTTP 409 on
claim rather than attaching to the existing session. On page unload the tab
releases the lease using a keepalive request. A heartbeat refreshes an active
lease; a stale lease expires after 120 seconds if the tab is closed without
releasing it. Reloading a page reacquires its existing session; if its prior
unload release has not finished yet, the frontend retries the claim briefly.

Each session has its own agent, MCP transport lifetime, conversation, tools and
contexts. A bounded in-memory user/assistant transcript is returned when
reopening a session; in-flight runs can be followed again via SSE. Sessions
and their transcripts are discarded when the server exits. Closing a tab does
**not** delete its session. The **Session beenden** button deletes only the current agent session and its
MCP connections. Other sessions remain active. To shut down the entire server,
press Ctrl+C in the terminal.

The built-in UI is launched only with the CLI option. Standalone
`cli-agent-api` remains a non-browser API server with its own bearer token
and its usual explicit port.

## Tests and limitations

`tests/test_web_ui_api.py` covers route authentication, context and tool
switches, history reset, session runs, setup commands and output writing.
`tests/test_web_ui_sessions.py` covers tab isolation, new-session tickets,
reopening, lease validation/expiry, and session cleanup.
The preexisting API tests cover SSE event replay, run isolation and approval
lifecycle. The API launcher is covered by isolated startup/cleanup tests, and the active
JavaScript is checked with Node.js (including cancellation of session deletion).
A manual browser smoke test (especially tool approval, page reload, and Ctrl+C
in the terminal) is still recommended in addition to CI.
