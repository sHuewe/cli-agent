# Web context and authenticated providers

The user-facing command `add_web_context <url>` loads web content. Users need not distinguish a public HTTP request from an administratively configured provider.

## Ordinary web URLs

Without a matching provider, the host must be included in the machine-wide `network.web_allowed_hosts` allowlist. Redirect destinations are checked against the same policy.

## Confluence Data Center with a personal access token

An administrator can configure a provider for an internal Confluence service:

```toml
[network]
web_allowed_hosts = ["confluence.internal.example.org"]

[[web.providers]]
type = "confluence"
base_url = "https://confluence.internal.example.org/wiki"
token_env = "CLI_AGENT_CONFLUENCE_PAT"
```

The base URL must use HTTPS; its host must also be allowed by the network policy. Optional context paths such as `/wiki` or `/confluence` are part of the provider identity. Provider matching requires the exact origin and path boundary.

The PAT is **not** stored in admin or user configuration. Only its environment-variable name is configured. For the current PowerShell process tree:

```powershell
$env:CLI_AGENT_CONFLUENCE_PAT = "<PAT>"
cli-agent
```

Matching Confluence requests use the REST API with `Authorization: Bearer <PAT>`. The PAT is never forwarded to an LLM, stored as web context or logged. Authenticated requests **never follow redirects**; each redirect response is rejected to prevent token disclosure. Request paths are strictly validated before sending: dot segments, backslashes and percent-encoded components are disallowed to prevent inconsistent URL normalization across clients, proxies and servers.

External stdio MCP subprocesses receive only their explicitly allowed environment variables; the PAT is not inherited automatically. Provider HTTP requests use `trust_env = false`, preventing environment-configured proxies from silently changing the route.

Supported Confluence page URLs include:

```text
.../pages/viewpage.action?pageId=12345
.../spaces/SPACE/pages/12345/Page+Title
.../display/SPACE/Page+Title
```

A page ID resolves via `/rest/api/content/{id}`. Legacy display URLs are resolved with space key and page title. The API requests `body.view` and `body.storage`; rendered `body.view` is preferred and reduced to text. If it contains no usable text, `body.storage` from the same REST response is considered.

A URL assigned to a configured provider must use that provider. If the PAT is missing, a REST call fails, or the URL cannot be resolved unambiguously, `add_web_context` returns an error. There is **no silent fallback** to unauthenticated HTML retrieval.

## Security boundary

Only machine-wide `admin_config.toml` may define authenticated providers and their credential variable names. User/project configuration cannot override them.

Retrieved content remains untrusted reference data even when authentication succeeds. Embedded instructions never gain permissions or trigger further network or tool calls.
