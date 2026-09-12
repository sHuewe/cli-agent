# Security Baseline

`cli-agent` is designed for local and on-premise use. It is not a guarantee
that a model, MCP server, or project is trustworthy. The shipped defaults
reduce accidental data disclosure and require an operator to make network
destinations explicit.

## Network policy

Model endpoints and streamable HTTP MCP servers are checked against separate
exact host allowlists. Their default allowlist contains only `localhost`,
`127.0.0.1`, and `::1`. Web context fetches are disabled by default; add the
exact internal hostname or IP address to the relevant list in `config.toml`.
Wildcard hosts are not supported.

```toml
[network]
model_allowed_hosts = ["localhost", "127.0.0.1", "::1", "llm.internal.example"]
mcp_allowed_hosts = ["localhost", "127.0.0.1", "::1", "tools.internal.example"]
web_allowed_hosts = ["docs.internal.example"]
```

Web redirects are validated one hop at a time. A redirect to a host outside
the allowlist is rejected before the next request is made. Model clients do
not follow HTTP redirects. Unencrypted HTTP is accepted only for local hosts;
remote allowlisted endpoints must use HTTPS.

## MCP process boundary

`stdio` MCP processes receive a minimal runtime environment. The complete
parent environment is not inherited, so tokens such as `LLM_API_KEY` are not
automatically exposed. An MCP server receives additional variables only when
they are explicitly configured in its `env` table. Treat such configuration as
secret-bearing and keep it out of version control.

Built-in execution and mutating tools require an explicit approval callback from
the embedding application. Every tool call belonging to a user-provided MCP
server requires approval, including tools whose metadata claims to be
read-only. The interactive CLI supplies a terminal prompt; non-interactive
invocations are denied by default. The approval summary hides file contents and
token-like arguments. Disabling a connected server also closes its transport;
re-enabling it creates a fresh connection. An embedding application must
provide an equivalent human approval mechanism before enabling tools.

Approval protects calls made through the agent, not arbitrary work performed by
an MCP process during startup or in the background. Configure only audited,
trusted stdio servers. User-provided stdio servers are refused unless their
configuration explicitly sets `allow_untrusted_stdio = true`; that setting is
only an acknowledgement, not a sandbox. Generic stdio processes still run with
the user's host permissions and require an additional OS/container sandbox in a
corporate deployment.

## Logs and context

Prompt logging, tool-call argument logging, model-message logging, and tool
result content logging are disabled by default. Context dumps are also
disabled in the shipped template. Enabling any of these options can persist
confidential source code, prompts, credentials, or tool output locally.

The workspace OS server refuses to read `.env*`, common credential files,
private-key files, `.git`/`.cli-agent` artifacts, log files, and files larger
than 1 MB. The repository ignores
`.cli-agent/`, environment files, coverage data, and
rotating log files by default. This does not remove files that were already
tracked or created before the ignore rule was added; remove those explicitly.

## Python validation

The Docker Python validator:

- uses `--network none` by default;
- runs as UID/GID `65532:65532` with a read-only root filesystem and a
  temporary writable filesystem;
- drops all Linux capabilities and enables `no-new-privileges`;
- creates a sanitized temporary project copy and mounts only that copy
  read-only; the original workspace is never mounted into the container;
- requires a pinned image digest, uses `--pull never`, and copies no `.env`,
  credential, private-key, `.cli-agent`, or log files into the container;
- does not use a shell to start the project.

Dependencies therefore must already be available in the image or through a
local package source. `--network-mode bridge` is an explicit opt-in for a
trusted, isolated validation run and must not be used for untrusted customer
code without an additional network control layer.

## Deployment checklist

1. Use an internal model endpoint and list its exact host in
   `model_allowed_hosts`.
2. Keep HTTP MCP and web context disabled unless their exact internal hosts are
   required and allowlisted.
3. Keep all write-capable MCP servers disabled for review-only use.
4. Keep diagnostic logging and context dumps disabled unless the workspace is
   approved for sensitive data.
5. Use a pinned, locally mirrored validator image and offline dependencies.
6. Run dependency and vulnerability scans against the lockfile before release.
7. Review and remove `.env`, `.cli-agent`, and log data created by older
   versions before sharing a workspace or support bundle.
8. Complete the [company deployment checklist](company-deployment-checklist.md)
   before a corporate rollout.
