# Python SDK (experimental)

The SDK is part of the regular `cli-agent` package, not a separate distribution.
The CLI programs `cli-agent`, `cli-agent-api`, and `cli-agent-flow` still
load the mandatory system-wide admin policy. SDK policy selection never changes
these programs.

## Jupyter / asynchronous applications

```python
from cli_agent.sdk import AgentBuilder

agent = (
    AgentBuilder()
    .with_admin_config(
        model_allowed_hosts=["localhost", "api.example.com"],
        mcp_allowed_hosts=["localhost"],
    )
    .with_project_config(
        workspace=".",
        model="qwen3.5:9b",
        provider="ollama",
        base_url="http://localhost:11434",
        workspace_access="read",
        with_data=True,
    )
    .build()
)

await agent.start()
try:
    first = await agent.ask("Analyze the available data.")
    second = await agent.ask("Check your previous analysis.")
    print(second.answer)
finally:
    await agent.close()
```

`async with agent:` is also supported. `ask` returns an `AgentResult`
with an `answer` string. The session retains conversation history and evidence
between calls. MCP connections live on a single worker task even when notebook
cells submit their prompts in separate tasks.

## Separate configurations

- No `with_admin_config` call: strict `AdminConfig()` defaults; the
  machine-wide policy is **not** read.
- `with_admin_config()`: explicitly load the system-wide admin policy.
- `with_admin_config(path="...")`: load that exact policy file; nonexistent
  explicit files are rejected.
- `with_admin_config(model_allowed_hosts=[...], ...)`: build a policy from
  strict defaults without reading any file.
- `with_admin_config(path="...", model_allowed_hosts=[...])`: load then
  replace the named settings.
- `with_project_config()`: load the regular default project config.
- `with_project_config(path="...")`: load a specific project config.
- `with_project_config(model="...", workspace="...", ...)`: start from
  project defaults and override the given settings without reading a file.

Host-list overrides **replace** the previous list. For richer policies,
`model_credentials`, `trusted_servers`, and `web_providers` accept the
respective existing configuration dataclasses.

The SDK does not sandbox arbitrary code running in the same Python interpreter.
It enforces the selected runtime policy against actions of the agent/LLM.
External tool registration from Python is reserved for a subsequent change.

## Structured progress, tool history and approvals

Console progress is opt-in; without a callback there is no console output:

```python
agent = (
    AgentBuilder()
    .with_project_config(workspace=".", workspace_access="read", with_data=True)
    .with_default_event_callback(verbose=False)
    .build()
)

async with agent:
    result = await agent.ask("Analyze the measurements.")
    print(result.answer)
    for call in result.tool_calls:
        print(call.name, call.status, call.duration_ms)
```

Use `.with_default_event_callback(verbose=True)` for more detailed progress,
or `.with_event_callback(my_callback)` for a custom synchronous or asynchronous
callback receiving an `AgentEvent`. Events have a per-request `run_id`,
`type`, optional `tool`, `call_id`, `status`, `phase` and
`duration_ms`. Every successful `AgentResult` includes the bounded
`events` and `tool_calls` tuples regardless of whether console output is
enabled. Events deliberately never include model prompts, tool arguments,
raw tool results or credentials.

A dedicated bounded event-consumer queue prevents a slow presentation callback
from blocking the MCP-owning worker. Consumer exceptions are logged, not
propagated to the active model/tool operation. A saturated callback queue may
drop live notifications; the independent run history is not affected until
its documented safety cap.

Approvals use an independent callback:

```python
async def approve(tool: str, arguments: dict) -> bool | str:
    return False  # default deny; True for once, "session" for this session

agent = (
    AgentBuilder()
    .with_project_config(workspace=".", workspace_access="write")
    .with_approval_callback(approve)
    .with_default_event_callback()
    .build()
)
```

Only `True` grants one call and `"session"` grants this tool for the
session. All other values deny the call. Without an approval callback,
approval-requiring actions are denied. The `approval_requested` and
`approval_decided` event metadata are observable but never grant access.


## Session reset, token usage and server controls

These methods are serialized with `ask()` on the same MCP-owning worker.
A reset queued during a request runs only after that request completes.

```python
async with agent:
    result = await agent.ask("Analyze the data.")
    print(result.usage.total_tokens)          # This request, if provider-reported
    print(result.session_usage.total_tokens)  # Since start() or last reset()

    for tool in await agent.list_tools():
        print(tool.name, tool.enabled, tool.approval_required)

    await agent.disable_server("os")
    await agent.enable_server("os")

    await agent.reset_history()  # Conversation, evidence, temporary artifacts
    await agent.reset()          # Also session approvals, tool/server state, usage baseline
```

Token counts are **not estimates**. When any model response within the
reported interval lacks usage metadata, the corresponding totals are `None`.
`model_calls`, `reported_calls`, and `unreported_calls` indicate
coverage. Sub-agent calls sharing the model usage log count toward the totals.
A full reset starts a fresh logical usage interval but does not erase the
underlying provider's historical accounting.

`list_tools()` only describes currently offered tools; it does not start
disabled external MCP servers. For each tool the SDK returns the exposed name,
server, description, input schema, enabled flag, and whether an approval is
required for that tool under the current session/policy.

`reset_history()` preserves existing approvals and server activation;
`reset()` clears session-only approvals, restores tool visibility and the
initial server/OKF activation state. Servers whose activation did not change
are not reconnected. A previously disabled server may need to reconnect
when restored to its initial enabled state. Neither reset alters AdminConfig.

## Read-only session state, JSON parsing and published artifacts

```python
async with agent:
    state = await agent.get_state()
    print(state.enabled_servers, state.history_length, state.approved_tools)

    result = await agent.ask("Return a JSON analysis and publish the CSV.")
    print(result.parsed)  # Parsed JSON if response_format="json"; otherwise None
    for artifact in result.artifacts:
        print(artifact.name, artifact.mime_type, artifact.size)
        artifact.save("reports")  # Existing directory or explicit new file
```

`get_state()` is serialized with requests. It returns metadata about history
length, enabled/disabled servers, approved/disabled tools, OKF and reported
usage. It never returns raw prompts, tool outputs or credentials.

`AgentResult.parsed` uses strict JSON parsing only when the session's
`response_format` is `json`. Text-mode responses retain `parsed=None`.
JSON object, list and scalar values are preserved without coercion.

`AgentResult.artifacts` contains only explicitly published MCP result
artifacts, never every temporary workspace file. They are validated through
the existing registered-artifact store and immediately snapshotted as bytes.
Consequently `read_bytes()` works even after later requests remove the
original temporary MCP files. `path` is `None` until the caller explicitly
uses `save(path)`, which uses exclusive creation and refuses overwriting.
To bound memory use, at most 32 published artifacts and 64 MiB of artifact
data may be snapshotted per request.

## Python functions as tools

Register annotated Python callables without running a separate MCP process:

```python
from cli_agent.sdk import AgentBuilder

def average(values: list[float]) -> float:
    """Calculate the arithmetic mean of numeric observations."""
    return sum(values) / len(values)

async def approve(tool: str, arguments: dict) -> bool | str:
    return tool == "python__average"

agent = (
    AgentBuilder()
    .with_project_config(workspace=".")
    .register_tool(average)
    .with_approval_callback(approve)
    .with_default_event_callback()
    .build()
)

async with agent:
    result = await agent.ask("Use the average tool for [2, 4, 6].")
    print(result.answer)
```

Registered functions appear as `python__<function_name>` and participate in
the normal tool schema validation, approval callback, tool events, and model
history. Without a callback or prior session approval, execution is denied.
Both synchronous and asynchronous functions are accepted. A synchronous
function runs in a worker thread; Python cannot forcibly interrupt a thread
that has already begun execution.

Python functions are **trusted application code running with the privileges
of the embedding interpreter**. No sandbox or special filesystem/network
restrictions are imposed on their bodies. The caller is responsible for
choosing functions to expose. Registration is per builder/agent instance,
never global. The model only receives the generated JSON schema and
description. Functions must accept named, type-annotated parameters and return
a JSON-serializable object or text; oversized results become tool errors.
