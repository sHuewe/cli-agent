# Security

`cli-agent` separates functional user/project configuration from machine-wide administrative security policy. This document describes the current hardening measures, their security guarantees and known residual risks. Historical findings and their complete original discussion are retained in [security.de.md](security.de.md).

## Review findings and mitigations

### Network / SSRF boundary

**Risk:** User-editable model, MCP or web URLs could reach arbitrary internal/external destinations. Redirects and proxy environment settings might circumvent host checks.

**Mitigation:** `model_allowed_hosts`, `mcp_allowed_hosts`, and `web_allowed_hosts` are defined only in machine-wide admin policy. URL validation uses exact normalized hostnames, rejects URL credentials, and requires HTTPS for nonlocal destinations. Model clients never follow redirects and use `trust_env=False`; each redirect in web retrieval is independently checked against the allowlist. Without an admin policy, model and HTTP MCP access are localhost-only and web access is off. See `tests/test_network_policy.py`, `tests/test_model_factory.py` and `tests/test_web_context.py`.

### Policy controlled by ordinary users

**Risk:** A security allowlist or process permission in the same user-editable `config.toml` as functional settings would not constitute an administrative boundary.

**Mitigation:** Security-sensitive networks, external stdio launch profiles and permanent tool auto-approvals are separately defined in `admin_config.toml`. Permanent approvals are additionally bound to an administratively specified MCP identity under `[[mcp.trusted_servers]]`. Policy is read only from fixed paths: `C:\ProgramData\cli-agent\admin_config.toml` (Windows) or `/etc/cli-agent/admin_config.toml` (Linux). User configuration cannot override it; missing admin files use restrictive defaults. The former `allow_untrusted_stdio` setting is unsupported.

### OKF as a separate local read boundary

**Intentional trust-boundary choice:** The optional OKF root is **not** bound to the project workspace. `[okf].repository` is user/project configuration; it may be an absolute path or outside the workspace. No administrative `okf_allowed_roots` restriction currently exists.

After resolution, the selected repository root is a fixed **read-only filesystem boundary**. Knowledge tools reject absolute follow-up paths, `..` traversal, symlink/reparse escapes and hardlink aliases. Retrieval has only the specified OKF read-only tools and accepts only follow-up paths offered by prior repository results.

Root selection itself is an **explicit local data-disclosure decision by the user**: selecting a repository outside the workspace can expose additional OKF content as reference context to the configured model. The threat model assumes users may explicitly supply locally accessible content to their approved LLM. A centrally managed list of permitted OKF roots is not planned for this design.

An arbitrary directory is not automatically a generic Markdown root. A readable `index.md` directly in the configured root is required as repository marker; startup does not recursively search deeper directories. Missing `index.md` fails closed. Nonconforming concept Markdown is not offered. The guarantee is containment **inside the user-selected root**, not restriction of the user's choice of root. See [OKF MCP](mcp-okf.md).

### Windows admin policy protection

**Risk:** Protecting only a policy file does not prevent a user from renaming/replacing it through a writable parent directory. ACL failures must not be overlooked.

**Mitigation:** `scripts/setup-admin-config.ps1` requires elevation, uses the fixed policy path rather than trusting `PROGRAMDATA`, rejects reparse points, takes ownership of the policy directory, resets its DACL, and applies well-known SID-based permissions. Administrators and SYSTEM receive full control; ordinary users receive read-only access. `takeown`/`icacls` errors are checked. Replacing an existing policy requires explicit confirmation or `-Force`. Developers with local administrative privileges can consciously elevate and change policy; this is an administrative trust decision, not a user-configuration feature.

### External stdio MCPs and process execution

**Risk:** Connecting to stdio MCPs launches a local process before tool approval occurs. Arbitrary user-configured commands are therefore too powerful even if every later tool call requires approval.

**Mitigation:** Every external stdio MCP must have its own `[[mcp.trusted_servers]]` launch profile in machine-wide policy. User/project configuration can select only its name; `command`, `args` and `env` are materialized from admin policy. User-defined launch arguments are rejected. Executable commands must be absolute paths or exactly `{python}`, never arbitrary bare PATH commands.

The agent resolves runtime placeholders such as `{workspace_directory}` itself, allowing workspace-bound MCP subprocesses without exposing a model-controlled workspace argument. stdio processes inherit a restricted environment; additional values are explicitly administrator-defined. `PYTHONSAFEPATH=1` is set, and `PYTHONPATH` is not inherited.

`--with-mcp NAME` selects an administratively defined MCP for the current process without modifying user config, repeatedly if needed. For stdio, launch properties remain administrator-owned; for HTTP, URL and headers come from the matching trusted profile. Unknown/mismatched identities and insufficient workspace grants fail before connection. Selection does not grant approvals or change network policy/instruction trust.

### Model credentials and static headers

**Risk:** Static `Authorization` inside user-controlled `[model.headers]` stores secrets in shareable/versioned TOML and risks accidental persistence.

**Mitigation:** Static Authorization headers are rejected case-insensitively in `[model.headers]`; other non-sensitive headers remain possible. OpenAI-compatible authentication uses `model.api_key_env`, with administrator constraints on **which environment-variable names** are permitted for nonlocal providers/hosts.

This does not validate the concrete secret value—users may change their environment variable's contents. It prevents project configuration from forwarding arbitrary existing process secret variables to permitted model hosts. This is credential hygiene, **not** an additional host/identity authorization mechanism. Tests: `tests/test_http_header_policy.py`.

### HTTP MCP credentials

**Risk:** Hard-coded Bearer headers store tokens in ordinary project config. Arbitrary environment expansion would also let a manipulated project config forward unrelated secrets to an allowed host.

**Mitigation:** Optional HTTP MCP bearer authentication is bound to a matching `[[mcp.trusted_servers]]` admin entry. `[mcp.trusted_servers.from_env.authentication]` contains a `bearer` property naming the allowed environment variable; its value contains only the raw token. The agent constructs `Authorization: Bearer <token>` at connection time. Missing/empty variables fail closed. Static Authorization headers are rejected in both user and trusted-server configuration. Unauthenticated HTTP MCP servers still work without a trusted profile unless other trusted features are needed.

### MCP tool metadata limits and accepted transport residual risk

External MCP tool names are untrusted metadata and are limited to **512 characters**, in addition to limits on tool count, instructions, descriptions, schemas and aggregate metadata.

**Verified residual risk (F-05, intentionally not fixed):** Review of locked MCP Python SDK `mcp==1.30.0` found that cli-agent's size limits apply **after** transport buffering and JSON/Pydantic materialization. The stdio transport builds a complete newline-delimited JSON-RPC message in a Python string before `model_validate_json(...)`. Ordinary HTTP JSON responses are fully read by `response.aread()`; SSE data are buffered in `sse.data` before JSON validation.

A compromised or malfunctioning **already approved** MCP may therefore consume substantial memory/CPU with very large messages before later metadata/result caps can reject them. Operation timeouts bound elapsed time, **not** a rapidly received response's size.

The residual risk is currently accepted rather than adding a fork, private SDK hook or custom replacement transport. External stdio needs an approved launch profile, HTTP destinations need an approved host, and built-in MCPs come from project source. The known impact is primarily availability/resource exhaustion within an already authorized MCP boundary; no corresponding privilege escalation or data exfiltration is known. Current limits still bound subsequent agent/prompt/log processing but are **not transport/preparse DoS limits**. Prefer a future official client-side response-size limit in the MCP Python SDK when available.

### MCP lifecycle and timeouts

MCP `initialize` and `list_tools` operations each have a **120-second** application deadline, and ordinary tool calls **600 seconds**. These generous ceilings guard against permanently stalled servers, including internal OKF MCP, by cancelling the pending await and returning an explicit runtime error.

### MCP instructions and prompt injection

External MCP `instructions` are server-controlled free text. They can be operationally useful, but automatically elevating them to system instructions increases their prompt trust.

By default, external instructions are **untrusted reference data** in a separate transient synthetic `user` message immediately before the real user request, together with OKF knowledge, web and local file context. The system prompt permits relevant operational/domain facts but forbids embedded instructions from changing the user's goal, permissions or security boundaries.

Synthetic reference context is not persisted to `self.history`. The real current user input remains a separate unchanged `user` message. For a specifically identified server, an administrator may explicitly allow `trust_instructions = true`; only then do its instructions enter the system prompt.

**Residual risk:** Textual separation cannot prevent all semantic model manipulation. Malicious web/file/OKF/MCP content may try to make the model use **already granted** capabilities. The technical guarantee is not that prompt injection becomes impossible, but that it does not create permissions, network destinations, workspace/repository access or approvals beyond the deterministic bounds already in place.

### Tool execution and MCP identity

External MCP tools require explicit interactive approval by default. Permanent administrator auto-approvals are scoped to `[[mcp.trusted_servers]]` and server identity: for HTTP, name, transport, full normalized endpoint and headers must match policy; for stdio, launch identity comes from the trusted profile. A pinned tool-contract SHA-256 includes native name, model-visible description and complete `inputSchema`. Drift returns execution to interactive approval.

Interactive users may approve an exact exposed tool for the running process with `[s]`. Trusted automation can preapprove exact names via repeatable `--approve-tool <exposed_name>`. No wildcard or `approve-all` is provided. Preapproval replaces only interactive confirmation, not server activation, admin rules, network, workspace or sensitive-path restrictions. Noninteractive execution without appropriate preapproval fails closed. Unknown tools, invalid JSON arguments and disabled-server tools are rejected before execution.

### Approval preview and argument redaction

**Risk:** A malicious external server controls argument names/schemas. Hiding values in approval previews merely because parameters are named `token`, `password`, `authorization` or `api_key` would let it conceal what data leave the host.

**Intentional mitigation:** Approval displays a bounded local preview of arguments **regardless of their names**. Large strings/collections are truncated to prevent an unbounded UI, but values are not hidden because of server-controlled names. This prioritizes visibility of outbound data over keeping potentially sensitive payloads off the local terminal. Approval contents are not additionally persisted as normal security logs.

Transport credentials are separate host-managed values from administrator configuration/environment; they are not model-generated tool arguments and ordinarily never appear in approval previews. Regression tests explicitly check misleading field names.

### Built-in OS writes

Built-in workspace OS MCP is read-only by default; `--with-os-write` explicitly enables mutating tools (`write_file`, `delete_file`, `make_directory`, `copy_file`), each still requiring approval. Administrative auto-approvals for external trusted MCPs cannot bypass this built-in rule. A user may explicitly approve one built-in write tool for the session or process via `--approve-tool`; others remain separately gated. Workspace containment and sensitive paths are never bypassed by approval.

### Workspace escapes and secrets

Workspace operations require relative, contained paths and reject traversal, absolute/Windows-drive paths and symlink escapes. Sensitive names such as `.env*`, credentials, keys, `.git`, `.cli-agent`, `.ssh`, `.aws` and logs are blocked for reads and mutation.

When the active user/project configuration file is inside the workspace, its exact path is additionally dynamically protected against reads, search/find, copies and mutations. `list_files` hides sensitive and dynamically protected items and denies direct listing of protected directories. `read_file` has bounded text sizes. Tests: `tests/test_os_operations.py`, `tests/test_mcp_server_entrypoints.py`.

### Sensitive output destinations

Although `--output` is explicitly user-chosen rather than model-controlled, it must follow the same sensitive-path categories as mutating workspace operations. `.env`, `.git`, `.cli-agent`, key/credential files and logs cannot be written even with `--overwrite-output`. Checks occur before agent/model construction. A necessary sensitive target name can still be created by an explicit manual move/rename outside the agent.

### Explicit prompt/context file size limits

`--context-file` and `--prompt-file` are read with a hard but generous **256 MiB per-file** binary limit before UTF-8 decoding. This is a local memory/DoS ceiling, **not** a promise of model capacity. The agent does not query the configured model's context size for this check; if the assembled prompt exceeds model capacity, the endpoint remains authoritative and its error is forwarded.

### Local runtime path leakage

The system prompt no longer reveals an absolute workspace path and instead requires relative paths for workspace tools. Runtime placeholders are transport-specific: administrator-controlled stdio launch profiles may use `{python}`, `{workspace_directory}`, `{project_directory}`, `{config_file}`. HTTP MCP URLs/headers may use only `{workspace_directory}` and `{project_directory}`; `{python}` and `{config_file}` are forbidden. Explicitly sending an absolute workspace path in an HTTP header to an approved remote host is a deliberate user/configuration disclosure decision, not a model-selected side effect.

### Web context and prompt injection

Web retrieval requires `web_allowed_hosts`, only textual HTTP responses are accepted, and both response bytes and model context are size-bounded. Redirect targets are independently validated. Retrieved content remains untrusted transient reference data, not persisted conversation history. One-shot `--add-web-context <url>` follows the same allowlist/redirect/size/untrusted-content path as interactive `add_web_context`. Tests: `tests/test_web_context.py`, `tests/test_cli.py`.

### OKF/Knowledge navigation and selection

The user-selected read-only OKF repository root may be outside the project workspace, but is itself a fixed filesystem boundary. The internal server checks against absolute paths, `..`, symlink/reparse escapes, hardlink aliases and excessive index/read sizes, and enforces Markdown/UTF-8 constraints.

Knowledge retrieval receives **only** internal `knowledge_index` and `knowledge_read`, and the agent accepts the internal OKF MCP only if it exposes exactly those tools. Normal main-agent tools and external MCP routes are unavailable in retrieval. Follow-up paths/`next_tool` pairs must have been offered in previous repository results. Duplicate or disallowed parallel calls are rejected, tool/concept budgets enforced, and final selection tokens verified against actually read concepts. Knowledge enters the main phase as transient untrusted reference context. Tests: `tests/test_okf_repository.py`, `tests/test_agent_knowledge.py`, `tests/test_agent_tool_calls.py`, `tests/test_agent_loop.py`.

### PDF worker environment

The isolated PDF parser receives a minimal environment allowlist and `PYTHONSAFEPATH=1`; cloud credentials, `PYTHONPATH` and other arbitrary parent environment variables are not inherited. The process provides timeout/lifecycle isolation, **not a full sandbox**.

### Logging, dumps and local artifacts

Prompt, tool argument, tool result, model-message logging and full context dumps are disabled by default. `.cli-agent/`, `.env*` and logs are Git-ignored; OS MCP protections deny access/mutation for these internal/sensitive artifacts. Diagnostic options intentionally remain privileged and may expose confidential data.

### Docker/code execution

Docker Compose and Python Validator were removed from core into separate `cli-agent-mcp`. The core no longer ships their entry points or direct Docker dependencies. Optional installation requires separate assessment and an approved external stdio launch profile.

### Optional local Web UI

**Threat model:** A browser UI for an agent with workspace and MCP capabilities must not introduce unintended remote control. Binding to all interfaces with `--with-os-write` could invalidate the local security boundary.

The original design (documented in the [historical version](security.de.md)) used optional `--with-web-ui`, fixed IPv4 loopback `127.0.0.1`, a random token in a URL fragment, a same-origin WebSocket and one browser connection at a time. The current API-backed browser architecture supports multiple sessions, browser lease ownership and AG-UI-style streaming; see [Web UI/API](web-ui-api.md) and [Agent API](agent-api.md) for the current details and tests.

The UI continues to use the same admin, network, MCP, workspace and sensitive-path rules as terminal execution, without its own weaker authorization path. `--approve-tool` is explicit process-scoped preapproval; pending interactive approvals fail closed on disconnect. Tool arguments use the same bounded preview.

For assistant responses, server-side `markdown-it-py` converts Markdown into HTML with raw HTML disabled (`html=false`) and automatic linkification disabled (`linkify=false`). Only generated HTML is inserted as assistant `innerHTML`; original response text remains available as content. User/system/error messages and tool arguments use `textContent`. No external scripts/styles/fonts/resources are fetched. CSP, `frame-ancestors 'none'`, `X-Content-Type-Options`, `Referrer-Policy` and restrictive Permissions Policy further reduce browser attack surface. Web dependencies are installed only through the `cli-agent[web]` extra.

### Dependencies and regression risks

Unused direct `cryptography` was removed (it may remain transitively via MCP/PyJWT). The `uv.lock` file pins resolved dependencies, and regression tests cover network/admin policy, MCP lifecycle, dispatch, approval, workspace operations and OKF. GitHub Actions runs pytest on Windows/Ubuntu with Python 3.11 and additional compatibility checks. A vulnerability/dependency scan remains an open release approval activity listed in [enterprise rollout checklist](company-deployment-checklist.md).

## Machine-wide admin policy

User/project configuration cannot define network allowlists or arbitrary external stdio processes. Each external stdio MCP must have a concrete `[[mcp.trusted_servers]]` entry in machine-wide `admin_config.toml`, which also controls permanent auto-approvals and `trust_instructions`.

- Windows: `C:\ProgramData\cli-agent\admin_config.toml`
- Linux: `/etc/cli-agent/admin_config.toml`

Without this file, model/HTTP MCP access is localhost-only; web, external stdio, external tool autoapprovals and external instruction trust are disabled. On Windows, `scripts/setup-admin-config.ps1` can configure ACLs in an elevated PowerShell. Normal users may read, but not change, policy. Trusted external server profiles are intentionally reviewed and added separately.

## Network

URLs use exact admin-allowlisted hosts. Remote hosts require HTTPS; HTTP is permitted only for local hosts. HTTP clients do not follow redirects automatically (web redirects are revalidated) and use `trust_env=False`.

## MCP

External stdio starts only with a matching admin `[[mcp.trusted_servers]]` profile using `transport="stdio"`. User config specifies the name only; admin config owns command/args/env. Unauthenticated HTTP MCPs need no trusted profile for ordinary usage, but their hosts must be in `network.mcp_allowed_hosts`. HTTP trusted profiles are needed for bearer authentication, privileged instructions or permanent tool autoapproval.

External tools require interactive approval unless a suitable exact-tool preapproval applies. Session/process approval does not bypass other policy. Built-in mutating workspace tools cannot be permanently approved via trusted-server autoapprovals. Ordinary MCP instructions remain untrusted context; only admin-trusted instructions enter the system prompt.

## Workspace OS

Paths are workspace-bounded; absolute paths, `..` and symlink escapes are rejected. Reads/mutations of secrets and internal agent/repository files are blocked. Write capability is disabled by default and requires `--with-os-write`, after which mutating tools still require approval unless deliberately preapproved for a session/process.

## Web context

Without `web_allowed_hosts`, web fetching is disabled. Retrieved context is bounded, untrusted, transient and cannot grant permissions or authorize new network access. Interactive and one-shot web context use the same safety mechanisms.

## Logging

Prompts, tool args/results, model messages and context dumps are not content-logged by default. Enable diagnostics only intentionally and treat the outputs as sensitive.

## Optional Docker MCPs

Docker Compose and Python Validator are maintained separately in `sHuewe/cli-agent-mcp`. Docker daemon/container execution risks are not part of core approval unless this optional package is separately installed and connected under an explicit administrative launch profile.
