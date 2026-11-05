# CLI Agent

A local command-line agent for LLM-assisted tasks with conversation history,
configurable MCP servers, optional read-only OKF/vector knowledge retrieval,
explicit web/file context, and repeatable multi-step workflows.

**Stable release:** [v1.0.0](https://github.com/sHuewe/cli-agent/releases/tag/v1.0.0).
Release artifacts include a wheel, source distribution, CycloneDX SBOM, and
SHA-256 checksums.

**Companion tools:** [cli-agent-utils](https://github.com/sHuewe/cli-agent-utils)
contains optional tools and reusable flows, independently maintained and reviewed
outside the core distribution.

> Documentation migration: English is now the project's canonical language.
> The previous detailed README is preserved as [README.de.md](README.de.md)
> while the remaining technical guides are translated. Historical security
> reviews are retained unchanged as audit records.

## Overview

```text
User -> CLI Agent -> [optional: OKF retrieval] -> LLM <-> MCP tools -> Answer
```

The agent can be used interactively, in one-shot mode, via its local HTTP
Agent API, or through the optional browser UI. It exposes only explicitly
enabled local capabilities and administratively permitted external services.

## Requirements and installation

Supported Python versions: CPython 3.11–3.14, on Windows x86-64 and Linux
x86-64/ARM64. These targets have prebuilt wheels for the security-critical
RE2 dependency. CI also tests Windows and Linux.

For an editable local installation on Windows:

```powershell
py -m pip install pipx
py -m pipx ensurepath
py -m pipx install --editable .
```

The standard user configuration is created on first run at
`%LOCALAPPDATA%\cli-agent\config.toml` on Windows. Model selection, MCP
servers, and optional capabilities are configured there.

The sample configuration is
[`src/cli_agent/settings/config.example.toml`](src/cli_agent/settings/config.example.toml).
The machine-wide policy example is [`admin_config.example.toml`](admin_config.example.toml).

## Configuration and trust boundaries

The ordinary user or project `config.toml` selects **what to use**.
The machine-wide `admin_config.toml` limits **what is permitted**. A user
configuration cannot override network allowlists, credential rules,
administrative MCP profiles, or other protected policy settings.

The admin policy is read from a fixed location:

- Windows: `C:\ProgramData\cli-agent\admin_config.toml`
- Linux: `/etc/cli-agent/admin_config.toml`

No admin file is required for local development. Without one, the default
policy restricts model and HTTP MCP access to localhost, disables web access
and external stdio MCPs, and grants no automatic approval for external tools.

On Windows, an administrator can create the policy using:

```powershell
Set-ExecutionPolicy RemoteSigned -Scope CurrentUser
.\scripts\setup-admin-config.ps1 -LlmHost "llm.example.org"
```

Model example:

```toml
[model]
provider = "openai"
model = "MODEL_NAME"
base_url = "https://llm.example.org/v1"
api_key_env = "LLM_API_KEY"
timeout = 120
context_length = 262144
```

The selected model host must be permitted by the admin policy. For a
nonlocal OpenAI-compatible model with credentials, the named environment
variable must also be permitted by an appropriate `[[model.credentials]]`
rule. Secrets belong in environment variables, not in static headers.

## Time zones

The current time in the system prompt uses the process's local time zone
unless an IANA time zone is configured:

```toml
[agent]
timezone = "Europe/Berlin"
```

This affects only the time included in the system prompt; it does **not**
change MCP parameters or data timestamps.

## MCP servers and permissions

External HTTP MCP servers can be selected in user configuration if their
hosts are allowed by admin policy:

```toml
[[mcp_servers]]
name = "example"
transport = "streamable_http"
url = "https://mcp.example.org/mcp"
```

Bearer authentication is configured through an identity-bound
`[[mcp.trusted_servers]]` entry in the admin policy, referring to a token
environment variable. Static Authorization headers are rejected.

External stdio MCP servers can only be selected by their administratively
assigned names; their launch executable, arguments, and environment are
controlled by the admin policy. Tools always remain subject to approval
and permission boundaries.

Enable trusted server profiles temporarily:

```powershell
cli-agent --with-mcp compose --with-os-read
cli-agent --with-mcp code-validator --with-os-write
```

Use `--approve-tool server__tool` to preapprove **one exact tool name** for
the current process. It is not a wildcard and does not activate a server
or bypass workspace, network, or administrative restrictions.

Read-only workspace access and write access are separately granted using
`--with-os-read` and `--with-os-write`. File paths in built-in workspace
tools are constrained to the selected workspace.

## Starting the agent

By default, the current directory is used as the workspace:

```powershell
cd C:\projects\my-project
cli-agent
cli-agent --config config.toml
cli-agent --with-os-read --with-data
```

`--with-data` alone enables the deterministic calculator. Tabular data
operations additionally need workspace read or write access. They support
common formats including Markdown, JSON, CSV, TSV, and JSONL/NDJSON.

The Data MCP keeps table-producing results in temporary session artifacts.
The model receives bounded metadata and previews rather than entire raw
datasets, along with verified artifact paths.

## Project-specific browser UI settings

The optional browser UI can be customized without changing its HTML or
JavaScript. Put the following in the workspace's
`.cli-agent/project.toml` (or in the ordinary user configuration):

```toml
[ui]
title = "SignalDeck AI"
default_language = "de"

[ui.buttons]
new_session = false
list_sessions = false
reset_history = false
show_working_context = false
end_session = true
language_selector = false

[ui.buttons.commands]
tokens = false
add_web_context = false
clear_web_context = false
enable = false
disable = false
```

All fields are optional. The default title is `cli-agent`; all controls
remain visible unless explicitly disabled. Without a configured default
language the browser's preferred language is used, as before. When the
language selector is visible, a previously saved manual selection takes
precedence over `default_language`. When the selector is hidden, the
configured default is used regardless of saved preferences (or the browser
language when no default is set). Languages must have a packaged translation
catalog (currently English and German).

Button settings control *visibility only*. They do not revoke server-side
permissions, remove API endpoints, or disable agent tools. Approval and
security policy remain enforced separately.

### Project-specific Jinja2 templates

The built-in UI is a packaged Jinja2 template. Set
`template = "ui/custom.html"` under `[ui]` in `.cli-agent/project.toml`,
and create `.cli-agent/ui/custom.html` (a UTF-8 regular file with no links).

Example:
```jinja2
{% extends "cli_agent/index.html" %}

{% block app_info %}
  <div class="project-info">
    {{ self.app_title() }}
    <p>Specialized AI assistant</p>
    {{ self.session_meta() }}
  </div>
{% endblock %}

{% block header_buttons %}
  <div class="header-actions">
    {{ self.button_language_selector() }}
    {{ self.button_show_working_context() }}
    <span>Project controls</span>
    {{ self.button_new_session() }}
    {{ self.button_list_sessions() }}
    {{ self.button_reset_history() }}
    {{ self.button_end_session() }}
  </div>
{% endblock %}
```

Nested extension blocks include `header_row`, `app_info`, `app_title`,
`session_meta`, `header_buttons`, `button_*`, `commands`,
`command_*`, `chat`, `composer`, `dialogs` and its individual
dialog blocks, as well as `extra_head` and `footer`. Use
`{{ self.name_of_block() }}` to reuse a component in a different order,
or `{{ super() }}` to retain and extend a parent block.

The JavaScript still relies on certain DOM elements being present exactly
once, even if a control is hidden. Rendered pages are validated before
serving. The normal Content-Security-Policy is preserved, so arbitrary
inline scripts and styles are not permitted. These project templates are
trusted local application configuration, not a safe way to render
untrusted user/LLM-controlled template source.

## Optional local browser UI

Install the optional web dependencies:

```powershell
pipx install "cli-agent[web]"
```

Start the UI with:

```powershell
cli-agent --config config.toml --with-os-read --with-web-ui
```

The browser uses the same agent capabilities and approval rules as the
terminal interface. The UI provides session management, chat, tool approval,
and inspection of model messages, tools, and active reference contexts.

The browser UI and Agent API are designed for localhost-only use and
do not provide a remote-access or reverse-proxy feature.

See [Browser UI](docs/web-ui-api.md) and [Agent API](docs/agent-api.md).

## Prompt files and templates

Prompt templates support named variables using `{{var:name}}`. Expansion
is nonrecursive: variable values are inserted once and are not interpreted
again as new template expressions. A leading backslash escapes a literal
placeholder. Templates do not include loops, conditionals, or implicit
environment-variable expansion.

## Multi-step workflows

`cli-agent-flow` executes deterministic, sequential workflows. Individual
steps can use separate model configurations and workspace access levels
without changing machine-wide policy.

```powershell
cli-agent-flow validate flow.toml --workspace C:\projects\my-project
cli-agent-flow run flow.toml --workspace C:\projects\my-project
```

Per-step tool preapprovals are exact names, just like CLI
`--approve-tool`. `foreach` can expand structured output from a previous
step while keeping workspace and security capabilities under static flow
configuration.

See [Workflow guide](docs/flow.md).

## Knowledge and external context

An OKF repository is an explicitly selected local read-only knowledge root:

```toml
[okf]
repository = "C:/knowledge/okf/bundle"
max_tool_calls = 200
max_read_bytes = 2560000
compress_min_chars = 20000
required = true
```

It may be outside the project workspace. The root must contain a readable
`index.md`; discovery does not recurse into unrelated directories. Only
the chosen repository is exposed for OKF knowledge retrieval. The user is
responsible for selecting data that may be shared with the configured model.

Web context is opt-in and subject to configured network permissions:

```powershell
cli-agent --add-web-context "https://docs.example.org/reference" "Summarize changes"
```

More details: [OKF MCP](docs/mcp-okf.md), [Vector knowledge](docs/knowledge-vector.md),
[File context](docs/file-context.md), and [Web context](docs/web-context.md).

## Security, architecture, and development

The security model separates user-requested actions from administrator
permissions and treats retrieved content and external MCP results as untrusted
data. For a deployment, review the security and networking documentation
before enabling external services or persistent tool preapprovals.

- [Architecture](docs/architecture.md)
- [Security](docs/security.md)
- [Deployment](docs/deployment.md)
- [Python SDK](docs/python-sdk.md)
- [CI and releases](docs/ci-release.md)
- [Historical German README](README.de.md)

Run tests with the locked development environment:

```console
uv sync --frozen --extra dev
uv run --frozen python -m pytest
```
