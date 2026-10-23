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
credential and can access only the active UI session and local UI endpoints.
Browser state-changing requests must carry the same-origin Origin header and
an explicit browser marker. Host validation and loopback-only binding remain in
place. The API mode does not write LLM context dumps.

## API communication

The frontend creates a run with `POST /sessions/{id}/runs`, receives
run events through `GET /sessions/{id}/runs/{run_id}/events` (SSE), and
sends approval decisions through the run-scoped approval endpoint. Existing
write-diff safeguards and fail-closed disconnect behavior remain server-side.

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

The built-in UI is launched only with the CLI option. Standalone
`cli-agent-api` remains a non-browser API server with its own bearer token
and its usual explicit port.

## Tests and limitations

`tests/test_web_ui_api.py` covers route authentication, context and tool
switches, history reset, session runs, setup commands and output writing.
The preexisting API tests cover SSE event replay, run isolation and approval
lifecycle. A manual browser smoke test (especially tool approval, page reload,
and Ctrl+C/shutdown) is recommended in addition to CI.
