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

The frontend creates a run with `POST /sessions/{id}/runs`, receives
run events through `GET /sessions/{id}/runs/{run_id}/events` (SSE), and
sends approval decisions through the run-scoped approval endpoint. For embedded
browser runs, the agent worker waits for an actual subscriber to the specific
run's SSE stream before entering `agent.ask()`. An abandoned run fails after a
bounded 15-second connection window rather than denying fast tool calls
because the stream is not yet connected. Standalone API runs continue
immediately and remain safe-by-default when approvals lack subscribers.
Existing write-diff safeguards and fail-closed disconnect behavior remain server-side.

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
Starlette server. Click **Neue Session in neuem Tab** to create a new session
and open it in a new tab. The **Sessions** dialog lists existing sessions and
allows opening unoccupied sessions and deleting idle sessions. The currently\nopen session can also be deleted from the dialog without terminating the server.

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
**not** delete its session. The **Sitzung beenden** button still shuts down the
entire local Web UI application, as it did before.

The built-in UI is launched only with the CLI option. Standalone
`cli-agent-api` remains a non-browser API server with its own bearer token
and its usual explicit port.

## Tests and limitations

`tests/test_web_ui_api.py` covers route authentication, context and tool
switches, history reset, session runs, setup commands and output writing.
`tests/test_web_ui_sessions.py` covers tab isolation, new-session tickets,
reopening, lease validation/expiry, and session cleanup.
The preexisting API tests cover SSE event replay, run isolation and approval
lifecycle. A manual browser smoke test (especially tool approval, page reload,
and Ctrl+C/shutdown) is recommended in addition to CI.
