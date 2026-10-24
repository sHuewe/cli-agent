# AG-UI 1.0 endpoint (cli-agent)

The canonical agent transport is **AG-UI 1.0 over HTTP/SSE**. Use this
endpoint from SignalDeck or a generic AG-UI client:

- URL: `POST http://127.0.0.1:<port>/ag-ui`
- `Authorization: Bearer <api-token>`
- `Content-Type: application/json`
- `Accept: text/event-stream`

The HTTP response has status 200 and `Content-Type: text/event-stream`.
Each SSE `data:` frame contains one standard AG-UI event. The stream ends
after `RUN_FINISHED` or `RUN_ERROR`. An interrupted run ends with an
AG-UI `RUN_FINISHED.outcome.type = "interrupt"`, not with a private approval
HTTP request.

Example request:

```json
{
  "threadId": "my-conversation",
  "runId": "e7b5d400-52cc-47d1-9cfa-80bfadd7e85c",
  "protocolVersion": "1.0",
  "messages": [
    {"id": "msg-1", "role": "user", "content": "Gib eine kurze Zusammenfassung."}
  ]
}
```

The `threadId` identifies a persistent MCP-owning agent session; a new thread
is created automatically on first use (subject to the global eight-session
limit). `runId` must be new on every run. The incoming `messages` list is
used to synchronize ordinary user/assistant conversation history. Application
`context` is included as **untrusted reference information**, never as a
server policy or an instruction with elevated trust. Multimodal parts that
cli-agent cannot interpret are not fetched/executed.

## Tool calls and approvals

Backend MCP tools are executed only by cli-agent and follow existing tool
restrictions. AG-UI provides the run and tool lifecycle events. If a tool
requires approval, the first run finishes with an interrupt (reason
`tool_call` and a `toolCallId`) and supplies a response schema containing
`approved: boolean`.

The client starts a **new** run on the same thread with an unchanged conversation
history and a `resume` response, for example:

```json
{
  "threadId": "my-conversation",
  "runId": "c38bcc46-526a-410f-bd3b-52d77ba9ecdb",
  "protocolVersion": "1.0",
  "messages": [
    {"id": "msg-1", "role": "user", "content": "Gib eine kurze Zusammenfassung."}
  ],
  "resume": [
    {"interruptId": "<id from previous RUN_FINISHED>",
     "status": "resolved", "payload": {"approved": true}}
  ]
}
```

A client may include `rememberForSession: true` in the payload to request
a session-wide approval; the server still validates the original policy
and write-target snapshot before granting it. `status: "cancelled"` safely
denies the pending operation.

MCP connections and the running agent coroutine remain on their original
worker task across interrupt/resume. A disconnected UI cannot make a tool
execute merely by leaving a stale approval in memory.

## Discoverable capabilities

`GET /ag-ui/capabilities` returns AG-UI's standardized
`AgentCapabilities` JSON shape. The specification does not assign a
discovery URL; this endpoint is a cli-agent convention, not an AG-UI wire
requirement.

Supported: HTTP/SSE, assistant text messages, MCP/backend tool lifecycle,
context input, thread continuity, human approval interrupts and resumes.

Supported frontend tools: The client advertises validated tools in
`RunAgentInput.tools`. The model may invoke them, the server emits
`TOOL_CALL_START` / `TOOL_CALL_ARGS` / `TOOL_CALL_END` and completes the run
with `RUN_FINISHED.outcome.type = "success"` and `pendingToolCallIds`. The UI
executes the tools and returns a `tool` message for **every** outstanding ID
on the next normal `RunAgentInput`. cli-agent never executes these tools
through MCP, and validates result-to-call correlation and tool-name collisions
before continuing.

Not advertised: multimodal model input, HTTP/Protobuf, resumable streams, and approve-with-edits.
AG-UI clients remain interoperable for the supported protocol features.
Supplying a frontend tool definition does not make cli-agent execute it.

The source of truth for protocol event shapes and CI schema tests is the
vendored official AG-UI 1.0 JSON Schema
(`src/cli_agent/ag_ui_schema_1_0.json`, upstream
`https://ag-ui.com/spec/1.0/schema.json`).

## Local cli-agent configuration

The built-in browser UI uses `/ag-ui` for normal messages and tool approval
resumes. Operations **not covered by AG-UI** remain separate:

- `/ui/command`: `tokens`, `add_web_context`, `clear_web_context`,
  `enable` and `disable`, with strict server-side validation.
- `/sessions/{id}/context`, `/sessions/{id}/working-context`,
  `/sessions/{id}/reset`, `/sessions/{id}/usage`: cli-agent-specific
  settings, diagnostics, history reset and token breakdown.
- `/ui/bootstrap`, `/ui/sessions`, `/ui/heartbeat`, `/ui/release`:
  browser-only session ownership and lifecycle.

`cli-agent-api` runs the API for an external AG-UI client. It listens only
on the local loopback address and requires the API bearer token. For an app
such as SignalDeck, pass the bearer token from the **backend plugin** rather
than exposing it in its frontend JavaScript.

`cli-agent --with-web-ui` starts the embedded UI and uses its own scoped
browser credential and exclusive per-tab session lease. Embedded browser
activity remains limited to at most four simultaneous runs to preserve
HTTP/1.1 connection capacity for control requests. Up to eight idle or
active independent sessions remain supported.
