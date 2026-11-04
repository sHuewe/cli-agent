# Local Agent API (experimental)

`cli-agent-api` manages independent local `CliAgent` sessions. The CLI mode
remains unchanged. Each API session shares the configuration fixed at server
startup (model, MCP servers, workspace, capabilities and admin policy), but has
its own conversation history, MCP lifecycle and tool approvals.

## Startup

```powershell
pipx install "cli-agent[web]"
cli-agent-api --config config.toml --with-os-read --port 8765
```

For explicitly trusted automation, selected tools may be preapproved
process-wide:

```powershell
cli-agent-api --with-os-write --approve-tool os__write_file --approve-tool external__search
```

`--approve-tool` may occur repeatedly; each entry matches an **exact exposed
tool name**, never wildcards. Unlike a session-scoped approval, it applies to
**all API sessions and arguments for that tool until server restart**. Other
tools continue to require API client approval. It does not enable OS access or
MCP servers, nor bypass admin, network, workspace or sensitive-path policy.

The server binds **only to 127.0.0.1** (no `--host` option). Startup prints a
random bearer token to stdout, required in the
`Authorization: Bearer <token>` header. Neither API nor token is intended as
a remote service; protect the token from disclosure in scripts or logs.

**Security boundary:** Loopback binding does not protect against local
proxies/tunnels, processes with equivalent OS permissions, or separate
network-exposed frontends proxying requests. The API rejects browser
Origin headers, permits only the configured agent, and rejects client-supplied
workspaces, MCP servers or permissions. If Open WebUI uses
`network_mode: host`, **Open WebUI itself** must be restricted to localhost.
Host networking does not provide a security boundary.

## Endpoints (v1)

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/sessions` | Create session |
| DELETE | `/sessions/{session_id}` | Close session and MCP connections |
| POST | `/sessions/{session_id}/runs` | Submit `{"prompt":"..."}`; receive run ID |
| GET | `/sessions/{session_id}/runs/{run_id}` | Run status and final answer |
| GET | `/sessions/{session_id}/runs/{run_id}/events` | SSE events, optional `Last-Event-ID` |
| POST | `/sessions/{session_id}/runs/{run_id}/approvals/{approval_id}` | `{"decision":"yes"|"no"|"session"}` |
| GET | `/sessions/{session_id}/context` | Active tools and contexts |
| PATCH | `/sessions/{session_id}/context` | Enable or disable existing tools/contexts |
| GET | `/health` | Authenticated health check |

All endpoints require the same bearer token and the `127.0.0.1` host.
Up to eight sessions are allowed simultaneously, with one active run per
session. Model responses are processed whole; **no token streaming**.

### SSE

Version 1 uses AG-UI event names `RUN_STARTED`, `RUN_FINISHED`,
`RUN_ERROR`, `TEXT_MESSAGE_START`, `TEXT_MESSAGE_CONTENT`,
`TEXT_MESSAGE_END`, `TOOL_CALL_START`, `TOOL_CALL_END` and
`CUSTOM` for approvals. Tool arguments and results are not generally
streamed, to minimize internal data disclosure. Unapproved tools use
`WebUiApprovalBroker`; approval is linked to the tool and run and is
denied without an active event subscriber.

**Compatibility:** This is an initial AG-UI-inspired event profile, not a
full AG-UI `RunAgentInput` endpoint. Standardized interrupt/resume,
canonical tool-argument events and generalized frontend state sync are
not supported. An Open WebUI pipe is not included. The purpose is to
establish local API and session lifecycle/approval boundaries.

## Initial non-goals

- No public binding or remote multiuser deployment.
- No token streaming or OpenAI Completions API.
- No durable sessions or restart recovery.
- No API changes to workspace, admin policy or MCP server configuration.
- No direct flows or arbitrary subprocess launches.

Tests: `tests/test_agent_api.py`.
