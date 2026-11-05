# Code architecture

**Current package structure and import paths:** See [`src/cli_agent/ARCHITECTURE.md`](../src/cli_agent/ARCHITECTURE.md) and the package-level README files. The historical flat `src/cli_agent` layout has been removed; no root-level alias modules remain.

This guide provides an overview of the current Python implementation: application entry points, state ownership, model/tool loops, and the boundaries where security and policy checks apply. It focuses on major components. Individual helpers and special cases are documented in their corresponding modules.

## Entry point and composition

The standard entry point is `src/cli_agent/interfaces/cli.py`, which:

1. Parses CLI arguments.
2. Loads user `config.toml` and machine-wide `admin_config.toml`.
3. Applies CLI overrides.
4. Creates the selected `ModelClient`.
5. Prepares MCP and approval configuration.
6. Validates file/prompt/output options.
7. Creates and starts the concrete agent.

The primary runtime agent is currently `ContextFileCliAgent`. For historical reasons, `cli.py` imports it under the local alias `WebContextCliAgent`:

```python
from cli_agent.context.file_context import ContextFileCliAgent as WebContextCliAgent
```

Actual inheritance:

```text
ContextFileCliAgent
        |
        v
WebContextCliAgent
        |
        v
CliAgent
  |- McpLifecycleMixin
  +- ConversationMixin
```

## Main agent classes

### `CliAgent` (`agent.py`)

`CliAgent` is the central agent state object. It holds the workspace/configuration path, `ModelClient`, configured MCP servers, logging configuration, network/MCP policy, conversation history, active MCP sessions, tool metadata/routes, enabled/disabled servers, trusted/untrusted MCP instructions, OKF/Knowledge state and session auto-approvals.

Shared helper logic includes system prompt assembly, placeholder expansion (e.g. `{python}`, `{workspace_directory}`), identity-bound trusted MCP checks, auto-approval contract verification, tool approval decisions and secure LLM context dumps. The main agent functionality is distributed across two mixins.

### `McpLifecycleMixin` (`agent_mcp.py`)

This mixin manages MCP connection lifecycle:

- Start and stop MCP servers.
- Establish stdio and `streamable_http` connections.
- Resolve external stdio name references to administrative `[[mcp.trusted_servers]]` launch profiles.
- Discover tools via `list_tools()`.
- Validate tool metadata and schemas.
- Generate exposed names in `server__tool` form.
- Map exposed tools to their `ClientSession` routes.
- Classify MCP instructions as trusted/untrusted.
- Enable or disable servers during a session.
- Start the separate internal OKF MCP server.

A conceptual tool route looks like:

```text
"search__start_search"
        |
        v
(ClientSession, "start_search", ServerConfig)
```

External stdio servers have an additional launch boundary:

```text
config.toml
[[mcp_servers]]
name = "compose"
       |
       v
admin_config.toml
[[mcp.trusted_servers]]
name = "compose"
transport = "stdio"
command = "..."
args = ["--project-directory", "{workspace_directory}"]
       |
       v
cli-agent materializes the admin profile
and resolves the agent's own workspace
       |
       v
stdio child process
```

Ordinary project configuration therefore cannot change the executable, argument structure or environment of an external stdio server. The agent binds the workspace itself; it is not a model-controlled tool parameter.

The mixin has no separate independent state; it operates directly on the `CliAgent` instance, including `_sessions`, `_tool_routes`, `_server_tools` and `_active_servers`.

### `ConversationMixin` (`agent_conversation.py`)

This mixin orchestrates one user request. Simplified flow of `ask(prompt)`:

```text
ask(prompt)
   |
   +-- optional Knowledge/OKF pre-run
   |      +-- _collect_knowledge(prompt)
   |
   +-- assemble transient reference context
   |      +-- untrusted MCP instructions
   |      +-- optional OKF knowledge
   |      +-- optional web context
   |      +-- optional local file context
   |
   +-- assemble working_messages
   |      +-- system
   |      +-- persistent history
   |      +-- optional synthetic user reference message
   |      +-- user: exact current prompt
   |
   +-- start model/tool loop
          +-- _run_model_loop(...)
```

The synthetic reference message is **not** persisted in `self.history`. After a successful turn, only the unchanged actual user request and the final assistant answer are retained. Conversation history and actual user input therefore remain separate from external reference data.

Trusted MCP instructions are the exception: built-in instructions and instructions from an identity-matched external server explicitly approved with `trust_instructions = true` are included in the system prompt. Other external instructions are still visible to the model, but only as untrusted reference data.

`ConversationMixin` also implements support for tool errors, result compression and OKF selection.

## Model/tool loop

The actual model/tool loop is implemented in `agent_loop.py` rather than the agent class:

```text
run_model_loop()
      |
      v
model_client.chat(messages, tools)
      |
      +-- final text response
      |      +-- return
      |
      +-- tool_calls
             |
             v
       process_tool_calls()
             |
             +-- add tool results to messages
                    |
                    +-- next model call
```

`agent_loop.py` controls iteration: call the model, classify its answer, dispatch tools, and continue conversation. For the Knowledge loop, it also guards against premature/invalid OKF selections and defines fallback behavior for repeated malformed responses.

## Tool calls and guardrails

Individual tool calls are processed by `agent_tool_calls.py`. They undergo checks including:

```text
LLM requests tool
        |
        v
Known tool name / route?
        |
        v
Server enabled?
        |
        v
Arguments valid JSON object?
        |
        v
Arguments match inputSchema?
        |
        v
Approval required?
        |
        v
Approved?
        |
        v
session.call_tool(...)
        |
        v
Result size limit / optional compression
        |
        v
Tool message returned to model context
```

Responsibilities are intentionally separated: `agent_loop.py` decides **when** models and tools run, while `agent_tool_calls.py` decides **whether and how** an individual tool call may execute.

## Specialized agents

### `WebContextCliAgent` (`web_context_agent.py`)

Extends `CliAgent` with explicitly loaded web contexts, the `add_web_context <url>` and `clear_web_context` local commands, insertion into the shared transient untrusted reference payload, separate token usage tracking for main and Knowledge loops, and the local `tokens` command.

URL validation/fetching is implemented by `web_context.py` and `network_policy.py`.

### `ContextFileCliAgent` (`file_context.py`)

Extends `WebContextCliAgent` with explicit local file context and host-side processing of `--context-file`, `--prompt-file`, `--output` and `--overwrite-output`.

File context is not an LLM tool call. It is validated explicitly by the host before agent execution, then included in the same transient untrusted reference payload as web, OKF and external MCP reference data.

## Model abstraction

`model.py` defines the `ModelClient` protocol:

```text
              ModelClient
               Protocol
              /        \
             /          \
    OllamaClient     OpenAIClient
                         |
                         +-- e.g. OpenRouter or other
                             OpenAI-compatible endpoints
```

Core interface:

```python
async def chat(messages, tools, *, think=None) -> dict
```

`model_factory.py` constructs the client based on `ModelConfig.provider`, checking allowed model hosts and credential variable policies. Model credentials are supplied through `api_key_env`; static `Authorization` headers in `[model.headers]` are rejected when user configuration loads. Administrator credential policy restricts the allowed **environment variable name** for a remote provider/host, not the secret value supplied by the user.

## Configuration and policy

Normal user configuration and machine policy are deliberately distinct:

```text
config.toml
    |
    v
 AppConfig
 |- ModelConfig
 |- LoggingConfig
 |- McpServerConfig[]
 +- OkfConfig

admin_config.toml
    |
    v
 AdminConfig
 |- NetworkConfig
 |- ModelCredentialRule[]
 +- McpPolicy
      +- TrustedMcpServer[]
```

User configuration mostly determines **what should be used**. Machine policy limits **what is permitted** for network destinations, external stdio launch profiles, credential sources and durable trust decisions.

The optional OKF root is an intentional exception: `[okf].repository` is user-selected, may lie outside the project workspace, and currently has no administrative root allowlist. After selection it forms a fixed read-only filesystem boundary for the internal Knowledge server. The architecture and security model treat OKF as a **separate local read boundary**, not an extension of workspace OS or an administrator-approved root list.

For HTTP MCPs, ordinary host approval via `mcp_allowed_hosts` is usually sufficient. A `TrustedMcpServer` entry is required for permanent auto-approvals or explicitly privileged instructions. External stdio MCPs always require `TrustedMcpServer` because launching them entails local process execution.

Configuration layers interact when constructing `ModelClient`, starting external MCP servers and checking permanent auto-approvals.

## Security boundaries

Principal trust relationships:

```text
User config ----+
                |
Admin policy ---+---> cli-agent
                |
LLM <-----------+
                |
MCP server <----+
                |
Workspace <-----+
                |
Web/file context+
```

Key assumptions:

- The LLM is **not a security boundary**.
- Web, file, OKF and unprivileged external MCP instructions are untrusted reference data.
- External MCP servers form their own trust boundary.
- Ordinary user configuration cannot extend administrative network, process launch or trust permissions.
- OKF root selection is explicitly separate: users configure the local read-only source they wish to share.
- Host-managed file access, especially built-in OS MCP, file context and output, is deterministically checked against the relevant workspace boundaries. External MCP servers are independent processes/services and their internal file or system access cannot be sandboxed to the workspace by `cli-agent`.

See [security.md](security.md) and [company-deployment-checklist.md](company-deployment-checklist.md).

## Mixins in the current design

`McpLifecycleMixin` and `ConversationMixin` are not generic reusable library mixins. Both are tightly coupled to `CliAgent` and access its attributes/helpers directly.

For example, they assume `self` provides `_tool_routes`, `model_client`, `history`, `logging_config` and `mcp_policy`. Python does not statically enforce this contract; missing state may surface only when a method executes.

These mixins are best understood as extracted partial implementations of `CliAgent`, splitting responsibilities and file size. If coupling grows substantially, explicit composition (`CliAgent` with an independent `McpManager` and `AgentRunner`) could be appropriate, but is not currently required for functionality.

## Navigating the code

A useful reading order:

1. `cli.py` — entry point and wiring.
2. `agent.py` — shared state and central policy helpers.
3. `agent_conversation.py` — `ask()` orchestration.
4. `agent_loop.py` — model/tool loop.
5. `agent_mcp.py` — MCP lifecycle/tool registration.
6. `agent_tool_calls.py` — concrete tool execution and guards.
7. `web_context_agent.py` and `file_context.py` — specialized context.
8. `model.py`, `model_factory.py` and provider clients — model access.

Supporting modules such as `network_policy.py`, `mcp_limits.py`, `mcp_contracts.py`, `filesystem_security.py` and `agent_knowledge*.py` implement specialized validation, security and knowledge logic.

## Overall architecture

```text
                         CLI
                          |
            User Config --+-- Admin Policy
                          |
                          v
               ContextFileCliAgent
                          |
                WebContextCliAgent
                          |
                      CliAgent
                    /          \
          MCP Lifecycle      Conversation
                |                 |
                |                 v
                |          run_model_loop()
                |                 |
                +--------> process_tool_calls()
                                  |
                                  v
                            MCP session.call_tool()

                        ModelClient
                       /           \
                  Ollama          OpenAI
                                (e.g. OpenRouter)
```

The architecture separates state management, MCP lifecycle, conversation orchestration, the agent loop, tool dispatch and concrete context/provider features, though parts are currently implemented using tightly coupled mixins.
