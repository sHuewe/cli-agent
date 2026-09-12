# Security Baseline

`cli-agent` is designed for local and on-premise use. It is not a guarantee that a model, MCP server, or project is trustworthy. The shipped defaults reduce accidental data disclosure and require network destinations to be explicit.

## Scope

The core package contains the generic agent/MCP integration, the Workspace OS MCP and the internal OKF retrieval server. Docker Compose and the Docker-based Python Validator are intentionally maintained in the separate `sHuewe/cli-agent-mcp` repository. Their Docker daemon and code-execution risks are therefore outside the core `cli-agent` security boundary unless an operator explicitly installs and configures those external MCP servers.

This separation is deliberate: many `cli-agent` use cases do not require Docker or arbitrary project execution, so those risks should not unnecessarily broaden a general approval of the core agent.

## Network policy

Model endpoints and streamable HTTP MCP servers are checked against separate exact host allowlists. Their default allowlist contains only `localhost`, `127.0.0.1`, and `::1`. Web context fetches are disabled by default.

```toml
[network]
model_allowed_hosts = ["localhost", "127.0.0.1", "::1", "llm.internal.example"]
mcp_allowed_hosts = ["localhost", "127.0.0.1", "::1", "tools.internal.example"]
web_allowed_hosts = ["docs.internal.example"]
```

Web redirects are validated one hop at a time. Model clients do not follow HTTP redirects. Unencrypted HTTP is accepted only for local hosts; remote allowlisted endpoints must use HTTPS.

## MCP process boundary

`stdio` MCP processes receive a minimal runtime environment. The complete parent environment is not inherited, so tokens such as `LLM_API_KEY` are not automatically exposed. Additional variables are passed only when explicitly configured.

User-provided MCP server metadata is not trusted as a security boundary. External stdio servers are refused unless their configuration explicitly sets `allow_untrusted_stdio = true`; that setting is an acknowledgement, not a sandbox. Under the current policy, calls to user-provided MCP tools require explicit operator approval.

Approval protects calls routed through the agent, not arbitrary work performed by an MCP process during startup or in the background. Configure only reviewed stdio servers and use an OS/container sandbox where appropriate.

## Workspace OS MCP

The Workspace OS server resolves paths below the fixed workspace, rejects absolute/drive paths and `..`, and prevents symlink escapes. Reads refuse `.env*`, common credential files, private-key files, `.git`/`.cli-agent` artifacts, log files and oversized files. Write access is not enabled by default and requires explicit `--with-os-write` activation.

## Logs and context

Prompt logging, tool-call argument logging, model-message logging, tool-result logging and context dumps are disabled by default. Enabling them can persist confidential source code, prompts, credentials or tool output locally.

## Deployment checklist

1. Use an internal model endpoint and list its exact host in `model_allowed_hosts`.
2. Keep HTTP MCP and web context disabled unless their exact internal hosts are required and allowlisted.
3. Enable only reviewed MCP servers required for the use case.
4. Keep write-capable MCP capabilities disabled for review-only use.
5. Keep diagnostic logging and context dumps disabled unless explicitly approved.
6. Run dependency/vulnerability scans and the automated test suite before release.
7. Assess optional MCP packages, especially Docker/process-executing servers, independently before installing or configuring them.
8. Complete the [company deployment checklist](company-deployment-checklist.md) before a corporate rollout.
