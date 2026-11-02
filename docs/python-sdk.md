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

## Configuration API: exact signatures and accepted options

The real Python method signatures in `cli_agent.sdk.builder.AgentBuilder` are:

```python
def with_admin_config(
    self, *, path: str | Path | None = None, **overrides: Any
) -> AgentBuilder: ...

def with_project_config(
    self, *, path: str | Path | None = None, **overrides: Any
) -> AgentBuilder: ...
```

Both accept keyword-only arguments. **The individual options below are
`**overrides` keys, not explicit parameters in the Python signatures.**
Unknown keys raise `TypeError`.

### Loading files versus constructing configurations in Python

| Call | Behavior |
| --- | --- |
| No `with_admin_config` call | Use restrictive `AdminConfig()`; do **not** read the machine-wide admin file |
| `.with_admin_config()` | Explicitly load the machine-wide admin file |
| `.with_admin_config(path="admin.toml")` | Load the specified admin file |
| `.with_admin_config(model_allowed_hosts=[...])` | Construct an admin policy from restrictive defaults, without reading a file |
| `.with_admin_config(path="admin.toml", model_allowed_hosts=[...])` | Load the file and replace the named fields |
| `.with_project_config()` | Load the normal default project config |
| `.with_project_config(path="config.toml")` | Load the specified project config |
| `.with_project_config(model="...", workspace="...")` | Construct from `AppConfig()` defaults, without reading the project config file |
| `.with_project_config(path="config.toml", model="...")` | Load the file and apply the specified overrides |

Administrative policy selection in the SDK does **not** change the mandatory
system policy used by `cli-agent`, `cli-agent-api` and `cli-agent-flow`.
Host-list overrides **replace** the existing list rather than appending.

### `with_admin_config(**overrides)`

Exactly these six keys are accepted:

| Key | Expected Python value | Purpose |
| --- | --- | --- |
| `model_allowed_hosts` | `list[str] \| tuple[str, ...]` | Hostnames permitted for LLM API requests |
| `mcp_allowed_hosts` | `list[str] \| tuple[str, ...]` | Hostnames permitted for networked MCP connections |
| `web_allowed_hosts` | `list[str] \| tuple[str, ...]` | Hostnames permitted for configured web providers |
| `model_credentials` | `list[ModelCredentialRule] \| tuple[ModelCredentialRule, ...]` | Whitelisted credential environment-variable names per model provider/host |
| `trusted_servers` | `list[TrustedMcpServer] \| tuple[TrustedMcpServer, ...]` | Trusted MCP server definitions |
| `web_providers` | `list[WebProviderConfig] \| tuple[WebProviderConfig, ...]` | Administrator-defined web provider connections |

The dataclasses `ModelCredentialRule`, `TrustedMcpServer` and
`WebProviderConfig` come from `cli_agent.settings.admin_config`.
Host lists contain **hostnames only** (for example `"openrouter.ai"`), not
URLs, schemes or paths. For external model API credentials, a matching
`ModelCredentialRule(provider="openai", host="...", allowed_api_key_envs=(...))`
is required in addition to `model_allowed_hosts`.

### `with_project_config(**overrides)`

The following keys modify project configuration. The model-related fields
correspond to `ModelConfig` in `cli_agent.settings.config`.

| Key | Expected Python value | Notes / defaults |
| --- | --- | --- |
| `provider` | `str` | `"ollama"` (default) or `"openai"` |
| `model` | `str` | Model identifier; default `"qwen3.5:9b"` |
| `base_url` | `str` | Model API root URL; default `"http://localhost:11434"` |
| `api_key_env` | `str \| None` | **Environment variable name**, not the secret; default `None` |
| `timeout` | `float` | Model request timeout in seconds; default `120.0` |
| `headers` | `dict[str, str]` | Extra model HTTP headers; `Authorization` is not accepted here |
| `context_length` | `int \| None` | Positive context size or `None` |
| `timezone` | `str \| None` | Agent's configured timezone |
| `mcp_servers` | `list[McpServerConfig] \| tuple[McpServerConfig, ...]` | Project MCP definitions |
| `okf` | `OkfConfig \| None` | Optional OKF knowledge configuration |
| `knowledge_vector` | `VectorKnowledgeConfig \| None` | Optional vector knowledge configuration |
| `logging` | `LoggingConfig` | Project logging configuration |
| `dump_llm_context` | `bool` | Enable local context diagnostics; default `False` |
| `max_pdf_pages` | `int` | Maximum PDF pages; default `100` |

These additional keys are accepted by `with_project_config`, but are
**SDK execution options**, forwarded to the runtime rather than stored as
`AppConfig` fields:

| Key | Expected Python value | Meaning |
| --- | --- | --- |
| `workspace` | `str \| Path` | Working directory; defaults to the process current working directory |
| `workspace_access` | `str \| None` | `"none"`, `"read"`, or `"write"` |
| `with_data` | `bool` | Enable built-in Data MCP |
| `with_mcp` | `tuple[str, ...]` | Named MCP servers to enable from admin configuration |
| `response_format` | `str` | `"text"` (default) or `"json"` |
| `context_files` | `tuple[Path, ...]` | Explicit local file contexts |
| `excluded_paths` | `tuple[Path, ...]` | Paths excluded from tool access |
| `mutation_protected_paths` | `tuple[Path, ...]` | Paths protected against changes |
| `retry_policy` | `ModelRetryPolicy \| None` | Model retry policy |
| `approval_callback` | `ApprovalCallback \| None` | Async approval function; prefer `.with_approval_callback(...)` |
| `user_message_callback` | `Callable[[str, str \| None], Awaitable[None]] \| None` | Messages sent to the user |
| `add_web_context` | `tuple[str, ...]` | URLs to supply as context during SDK initialization |
| `dump_file_prefix` | `str \| None` | Diagnostic dump filename prefix |

The configuration dataclasses are defined in
`cli_agent.settings.config`. `ModelRetryPolicy` is defined in
`cli_agent.models.model`. The builder validates model and policy settings;
values forwarded as runtime options are also subject to runtime validation.

### API keys: external OpenAI-compatible API (example: OpenRouter)

**Do not pass the secret as `api_key`, and do not embed it in
`base_url` or `headers`.** The supported mechanism is `api_key_env`.
Set the environment variable in the shell or your secret manager **before
starting the agent**:

```bash
# Linux/macOS; set the real value in your environment, not in source control:
export OPENROUTER_API_KEY="your-secret-from-your-secret-manager"
```

PowerShell equivalent: `$env:OPENROUTER_API_KEY = "your-secret"`.
For example, if the variable has been supplied by your environment:

```python
from cli_agent.sdk import AgentBuilder
from cli_agent.settings.admin_config import ModelCredentialRule

agent = (
    AgentBuilder()
    .with_admin_config(
        model_allowed_hosts=["localhost", "openrouter.ai"],
        model_credentials=[
            ModelCredentialRule(
                provider="openai",
                host="openrouter.ai",
                allowed_api_key_envs=("OPENROUTER_API_KEY",),
            )
        ],
    )
    .with_project_config(
        provider="openai",
        model="openai/gpt-4o-mini",
        base_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
        workspace=".",
        workspace_access="read",
    )
    .build()
)

async with agent:
    result = await agent.ask("Hello")
    print(result.answer)
```

The two layers have separate responsibilities:

- **Project config:** `api_key_env="OPENROUTER_API_KEY"` tells the model
  client which environment variable to read. It does **not** contain the key.
- **Admin config:** `model_allowed_hosts` allows the endpoint;
  `model_credentials` authorizes sending that named variable to the exact
  `provider="openai"` / `host="openrouter.ai"` combination.

The library reads the environment variable when constructing the model
client during `agent.start()`. If it is unset or empty, startup fails.
If the external host is not allowed, or its credential variable is not
whitelisted for the provider/host, startup also fails. No secret is stored
in the project TOML or the builder arguments.

For the local Ollama default, no API key is needed:

```python
agent = (
    AgentBuilder()
    .with_project_config(
        provider="ollama",
        model="qwen3.5:9b",
        base_url="http://localhost:11434",
    )
    .build()
)
```

Localhost model endpoints do not require a remote-host
`ModelCredentialRule`. If using an OpenAI-compatible **local** model
endpoint that requires authentication, set `provider="openai"` and
`api_key_env="NAME_OF_LOCAL_KEY_VARIABLE"`; localhost is exempt from
the remote credential allowlist.

### Other builder signatures

```python
def with_event_callback(self, callback: Any) -> AgentBuilder: ...
def with_default_event_callback(self, *, verbose: bool = False) -> AgentBuilder: ...
def with_approval_callback(self, callback: Any) -> AgentBuilder: ...
def register_tool(
    self, function: Any, *, name: str | None = None,
    description: str | None = None,
) -> AgentBuilder: ...
def build(self) -> Agent: ...
```

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

## Add arbitrary Python values as reference context

`await agent.add_context(value, *, name=None)` accepts **any object**
that can be converted with `str(value)`. It returns a stable context ID,
for example `"sdk:0"`. The conversion happens when the method is called;
the original Python object is not retained or watched for later changes.

```python
async with agent:
    await agent.add_context({"measurements": [3, 5, 8]}, name="sensor-data")
    await agent.add_context(my_dataframe.to_string(), name="table")
    result = await agent.ask("Evaluate these reference measurements.")
```

The value is included in the same **untrusted reference-context data layer**
used by file and web context. It is not a system prompt and cannot override
the runtime's instructions or approvals. Contexts stay active across subsequent
`ask()` calls, including `reset_history()` and `reset()`, until the
agent is closed. Calls are queued behind any in-progress request.

An ordinary string containing a URL remains a string: `add_context`
**does not fetch web pages or read file paths**. Use `context_files`
and `add_web_context` for explicit file/web loading.

To avoid unbounded model-context growth the SDK limits values to 128 KiB
each, 32 contexts, and 512 KiB combined. Strings are not truncated
silently: oversized input raises `ValueError`.
