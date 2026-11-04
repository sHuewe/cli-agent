# Follow-up to the independent security review

This document records technical mitigations derived from independent reviews of
the `security-hardening-review` branch. General security boundaries are described
in [security.md](security.md).

This is a **historical record of review findings and mitigations**. Some behavior
has evolved since these reviews; in particular, the approval-preview redaction
described in the September 13 follow-up was subsequently changed. Refer to
[security.md](security.md) for the current policy, rather than interpreting older
review entries as current normative behavior. The original German text is
preserved as [security-review-followup.de.md](security-review-followup.de.md).

## F-01: Workspace shadowing of built-in MCP modules

**Status:** Resolved.

Built-in stdio MCP processes set `PYTHONSAFEPATH=1` and do not inherit
`PYTHONPATH`. Thus a repository-local `cli_agent` package cannot replace
the installed built-in MCP implementation merely through the working directory.
Regression tests cover the environment and a real Python subprocess.

## F-02: MCP instructions as privileged model context

**Status:** Selectively resolved and functionally extended.

External MCP server instructions are no longer automatically placed in the
system prompt. An administrator may explicitly trust instructions from a
specifically identified and reviewed MCP server:

```toml
[[mcp.trusted_servers]]
name = "fachsoftware"
transport = "streamable_http"
url = "https://mcp.intern.firma.de/mcp"
trust_instructions = true
```

This privileged trust decision is bound to the same concrete server identity
as permanent auto-approvals. An HTTP MCP with the same name but a different
endpoint or headers does not inherit it. For stdio, the entire launch identity
is already determined by machine-wide policy. Built-in MCPs remain trusted
as part of the managed application.

Instructions without administrative trust are not simply discarded. They are
provided as explicit **untrusted reference context** in a separate transient
`user` message immediately before the actual current user request. The same
synthetic message may also contain OKF knowledge, web context and explicitly
loaded local file context. The system prompt permits their domain or operational
information to be used but forbids embedded instructions from overriding
system/user rules or permission boundaries.

The synthetic reference message exists for the current model run only and
is **not added to conversation history**. `self.history` retains only
actual user requests and assistant replies; the current user request also
remains a distinct, unmodified `user` message in `working_messages`.

## F-04: Arbitrary process environment variable as remote LLM credential

**Status:** Resolved.

When `api_key_env` is used for a nonlocal OpenAI-compatible model endpoint,
machine policy must bind the eligible credential environment variable to
the provider and destination host:

```toml
[[model.credentials]]
provider = "openai"
host = "llm.intern.firma.de"
allowed_api_key_envs = ["LLM_API_KEY"]
```

Ordinary project configuration can no longer select a variable such as
`AWS_SECRET_ACCESS_KEY` as credentials for a remote LLM host that was
only approved as a network destination. `api_key_env` itself remains
optional: without it, the OpenAI-compatible client sends no
`Authorization` header. If configured, the named environment variable
must exist and be nonempty; otherwise model configuration is rejected.
Local model targets deliberately remain flexible and may run without an
API key.

## F-05: Trusted stdio and PATH resolution

**Status:** Resolved.

An administratively trusted stdio MCP cannot be identified using a bare
command such as `python`, `node` or `my-mcp`. Machine policy accepts
only an absolute executable path or the special `{python}` placeholder,
deterministically resolved to `sys.executable` of the active cli-agent
installation.

## Additional hardening: external stdio MCPs require admin launch profiles

**Status:** Resolved.

The former global machine option `allow_untrusted_stdio` was removed.
Such a switch was too broad because it would allow ordinary project
configuration to launch arbitrary local executables as stdio MCP processes.
Tool approvals happen only **after** process launch and therefore cannot
protect that initial execution.

Each external stdio MCP must instead have its own machine-wide
`[[mcp.trusted_servers]]` definition. Ordinary user/project configuration
references such a server solely by name:

```toml
# config.toml
[[mcp_servers]]
name = "compose"
```

Full launch configuration is stored exclusively in admin policy:

```toml
# admin_config.toml
[[mcp.trusted_servers]]
name = "compose"
transport = "stdio"
command = "C:/Program Files/Company/compose-mcp.exe"
args = ["--project-directory", "{workspace_directory}"]
```

`command`, `args` and `env` cannot be changed by project configuration.
The agent resolves controlled runtime placeholders such as
`{workspace_directory}`. This preserves the desired one-to-one mapping
between agent, workspace and a workspace-bound MCP subprocess without
exposing the workspace as a model-controlled tool parameter.

Streamable HTTP MCPs still do not require a trusted-server entry as long
as their host is allowed by `network.mcp_allowed_hosts` and neither
permanent auto-approvals nor `trust_instructions = true` is required.
Their tool calls still require interactive approval by default, and their
instructions remain untrusted reference context.

## F-06: HTTP headers affecting routing

**Status:** Resolved for user-configurable headers.

User configuration cannot set routing/proxy-related headers such as
`Host`, `:authority`, `Forwarded`, `X-Forwarded-*`,
`X-Original-Host`, `Proxy-Authorization`, `Proxy-Connection`,
`Connection` or `Upgrade` for model or HTTP MCP connections.
Host and routing therefore derive from the URL already checked against
the administrative allowlist.

Web redirects remain possible only if **every** redirect destination
is itself allowed by `web_allowed_hosts`. Additional DNS/IP-based
network restrictions remain optional defense in depth.

## F-07: Unbounded MCP metadata and tool results

**Status:** Resolved with deliberately generous safety limits.

Fixed limits apply to tool counts, server instructions, individual tool
descriptions/schemas, aggregate server metadata and the text size of a
single tool result before it is forwarded to a model or compression stage.

These intentionally generous upper bounds are designed as an emergency
brake against broken or compromised servers:

- At most **1,024** tools per MCP server.
- At most **2,000,000** characters of server instructions.
- At most **250,000** characters per tool description.
- At most **2,000,000** characters per tool input schema.
- At most **20,000,000** characters of total metadata per server.
- At most **10,000,000** characters per tool result.

## F-08: Freely configurable log file path

**Status:** Pragmatically resolved.

Project configuration cannot redirect logging to an arbitrary absolute path.
When `[logging].file` is set, it must be relative to the normal cli-agent
state directory. Absolute Windows/POSIX paths, `..` traversal and resolved
destinations outside that directory are rejected.

## F-10: CI did not enforce the lockfile

**Status:** Resolved for CI/review.

CI installs an exact `uv` version, validates `uv.lock` using
`uv lock --check`, synchronizes project and test dependencies with
`uv sync --frozen --extra dev`, then runs pytest in this locked
environment.

## Additional hardening: contract pinning for permanent MCP auto-approvals

**Status:** Implemented and extended to tool descriptions.

Permanent MCP auto-approval is bound to server identity and the entire
model-visible tool contract. The contract fingerprint is a SHA-256 hash
over canonical JSON containing the native tool name, normalized tool
description and complete current `inputSchema`.

A description is security-relevant because it tells the model how and why
to use the tool. A semantic description change can alter model behavior
even when the name and callable schema remain identical. Such changes
invalidate prior permanent approval and require ordinary interactive
approval again.

To avoid treating irrelevant text formatting as contract drift, descriptions
are narrowly normalized before hashing: `CRLF` and `CR` become `LF`,
trailing spaces/tabs on lines are removed, and blank lines at the end
are ignored. Leading indentation, internal blank lines, capitalization,
punctuation, Markdown and all other content differences remain significant.

Therefore a prior approval automatically becomes ineffective after an
MCP update that changes the tool's meaning, adds a parameter, changes
parameter types, or modifies required fields. The contract uses version
**2**; older fingerprints no longer apply.

The CLI offers read-only helper commands:

```text
cli-agent admin inspect-tool <server> <tool> --config <config.toml>
cli-agent admin trust-tool <server> <tool> --config <config.toml>
```

`inspect-tool` shows the currently exposed contract, including description
and input schema. After the same checks, `trust-tool` prints a copyable
TOML block. Neither command writes to `admin_config.toml`; the
administrator's trust decision remains an explicit copy/review step.
`--update` compares an existing pinned contract with the current version
and creates an updated replacement block.

Regression tests confirm that changes to the tool schema, tool name or
description invalidate approval, while line ending, trailing whitespace
and final blank-line variations produce the same fingerprint.

# Follow-up to the September 13, 2026 review

## Hardlink aliasing in workspace and OKF

**Status:** Resolved.

Existing regular files with more than one hardlink (`st_nlink > 1`)
are rejected for security-relevant workspace operations, including
reads, writes, deletes, copy sources and existing copy targets.
The OKF reader likewise rejects hardlinked Markdown. A harmless-looking
workspace path therefore cannot serve as an alias for the same filesystem
object as a sensitive or external file.

Regression tests create real hardlinks and check fail-closed behavior for
read, write, copy and OKF access.

## Context dumps under `.cli-agent`

**Status:** Resolved.

`dump_llm_context` validates both the dump directory and existing dump
files before every write. `.cli-agent` must not be a symlink or Windows
reparse point, and must resolve inside the workspace. Existing dump files
must not be symlinks/reparse points or multiply hardlinked. Manipulated
project structures thus cannot redirect context dumps to other paths.

## Information displayed in tool approval

**Status at this review:** Resolved. **Superseded by later policy.**

At the time of this review, approval previews masked credential-like
fields such as `token`, `password`, `api_key`, `authorization`,
`credential` and `secret`, including nested keys. Generic payload
fields such as `content`, `body`, `data` or `payload` stayed visible
so users could assess the write or transfer they were approving.

Long strings were bounded: up to **2,000** characters were shown in full;
longer strings displayed their start and end with an explicit count of
omitted characters. Large collections were likewise limited. The goal
was to preserve relevant approval information without unlimited output.

**Current behavior differs:** See [security.md](security.md), which documents
the subsequent decision not to hide values based on MCP-controlled field names.

## Web URLs in INFO logs

**Status:** Resolved.

When loading web context, INFO logs include only scheme, host/port and path
for `requested_url` and `final_url`. Query and fragment are omitted.
Tokens, signatures or other secrets in query parameters therefore do not
leak through this log entry.
