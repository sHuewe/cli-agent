# Intended use and scope boundaries

> As of October 6, 2026. Statements about other projects reflect their publicly documented capabilities as of that date and should be reverified before future product decisions.

## Design objective

`cli-agent` is deliberately **not** intended to be a maximally autonomous coding agent. Its focus is a **locally deployable, policy-controlled LLM/MCP agent for technical workflows**, exposing only the capabilities genuinely required, as explicitly, narrowly and verifiably as practical.

A typical use case expects:

- A privately operated or internally provided OpenAI-compatible LLM endpoint.
- MCP as a defined interface to additional functions and domain systems.
- Technically enforceable administrative constraints that ordinary user configuration cannot relax.
- No generic shell tool or arbitrary model-driven code execution.
- No persistent cross-project agent memory in core.
- Explicitly bounded workspace, network and tool access; optional knowledge sources have separately documented trust boundaries.
- Auditable, reproducible execution for recurring tasks.

The design question is therefore not **what an agent can theoretically do**, but **which capabilities are actually needed for a concrete application and how narrowly they can be technically restricted**.

## Deliberate non-goals

The core does not expose a universal execution primitive like `shell(command)` or a freely programmable code-execution environment to the model.

Instead, main-agent workspace filesystem operations use defined built-in MCP tools with their own path validation, workspace containment, size limits, sensitive-path protection and separate read/write capabilities. This statement intentionally does **not** apply universally to optional local context sources: the separately enabled OKF knowledge server has its own read-only repository root, which may reside outside the workspace. External MCPs may add other capabilities and need separate assessment and administrative approval for managed use.

The aim is to prevent one generic capability from indirectly reopening many other security boundaries. For example, a shell combines file, network, process and code execution. A narrowly defined `read_file` or domain-specific MCP tool has a much smaller, more easily audited contract.

## Administrative security boundary

`cli-agent` separates normal user/project configuration from machine-wide administrative policy.

User configuration determines **what to use**; admin policy determines **what is permitted at all**. The latter covers allowed model/MCP/web destinations, external stdio MCP launch profiles, credential bindings and optional permanent tool auto-approvals.

Key characteristics:

- Admin policy is loaded only from a fixed machine-wide path.
- User configuration cannot relax its constraints.
- Absent policy results in restrictive defaults.
- A present but invalid policy fails closed instead of silently falling back to weaker settings.
- External stdio MCPs are not launched using arbitrary user-provided commands; their launch profiles come from admin policy.
- Model, MCP and web network destinations have separate administrative allowlists.

See [Security](security.md), [Deployment](deployment.md) and the [enterprise rollout checklist](company-deployment-checklist.md).

### OKF as a separate local read boundary

Optional OKF knowledge access is an intentional exception to the workspace boundary. `[okf].repository` belongs to user/project configuration and may point to a root outside the workspace. After selection, Knowledge tools are deterministically read-only and contained to that root: absolute follow-up paths, `..`, symlink/reparse escapes and hardlink aliases are blocked, and retrieval offers only the specified OKF tools.

**Selecting the OKF root is deliberately user configuration, not admin policy.** There is no `okf_allowed_roots` list in `admin_config.toml`, nor is one planned for the current trust model. By setting `[okf].repository`, the user explicitly selects a local knowledge source whose relevant content may be provided to the configured LLM. This follows the assumption that users may independently provide locally accessible information as context to their approved model.

The technical security boundary is therefore not an administrative list of permitted repository paths, but **containment within the explicitly selected OKF root**. After selection, retrieval cannot escape it and has only the two read-only tools `knowledge_index` and `knowledge_read`. The internal OKF MCP is accepted only if it exposes exactly that tool set.

There is **no unrestricted file browser even within the selected root**. The directory must be a valid OKF repository with a readable root `index.md`. Both tools follow OKF structure only; nonconforming Markdown files are not offered as knowledge and are rejected by `knowledge_read`. Merely entering an arbitrary local directory path does not make it a freely searchable data source.

Two distinct boundaries follow: Workspace OS MCP cannot escape its workspace; OKF MCP cannot escape the **repository root explicitly selected by the user**. The absent administrative root allowlist is a deliberate design choice, not an outstanding mitigation.

See [OKF MCP](mcp-okf.md) and [Security](security.md).

## MCP as a controlled capability boundary

For `cli-agent`, MCP is not merely an extension mechanism, but an intentionally controlled capability boundary.

External tools require approval by default. Permanent auto-approvals are optional and are pinned not just to a tool name, but to the administratively defined server identity and a SHA-256 fingerprint of the tool contract. That contract contains the tool name, model-visible description and full `inputSchema`. Contract changes fall back to ordinary user approval.

MCP instructions also have a trust boundary. External server instructions are treated as untrusted reference context unless an explicit administrative `trust_instructions = true` approval allows inclusion in the system prompt.

## Reproducible rather than arbitrarily autonomous workflows

For recurring work, `cli-agent` supports declarative multi-step flows. Individual steps can define model, file/web context, workspace access, tool preapprovals, retries and response format.

Capabilities can therefore be limited to what each step actually needs. `foreach`, checkpoints, subflows and conversation steps allow more complex workflows without granting a generic shell or freely programmable agent execution.

## Comparison with established open-source agents

The following projects are technically capable but sometimes deliberately pursue different goals. This comparison is **not** an overall quality ranking; it assesses suitability where a custom LLM endpoint, MCP and a small administratively constrained capability surface matter most.

| Project | Custom/OpenAI-compatible LLM endpoint | MCP | Generic shell / code execution | Governance implications |
| --- | --- | --- | --- | --- |
| **cli-agent** | Yes | Yes | **Not a core capability** | Fixed machine-wide admin policy, fail-closed invalid policy, explicit capability limits |
| **Hermes Agent** | Yes | Yes | Terminal and code execution are documented features | Toolsets can be disabled and configuration pinned in managed scope, but managed scope is not described as a hard sandbox and needs additional deployment hardening |
| **goose** | Yes | Yes | Developer Extension includes a `shell` tool for arbitrary commands with user privileges | Tool permissions support `Never Allow`; public controls are mainly user/session configuration |
| **Continue** | Yes | Yes | Built-in agent tool `Bash` | `Bash` can be excluded, but CLI modes/flags take precedence over personal `permissions.yaml` |
| **OpenHands** | Yes | Yes | Persistent terminal is part of agent SDK | Oriented toward coding agents and sandboxing; excluding all generic shell execution requires additional adjustments |

### Hermes Agent

Hermes supports custom OpenAI-compatible endpoints and offers terminal, file tools, code execution, memory, skills and additional toolsets.

`agent.disabled_toolsets` can disable toolsets globally. Managed Scope can give administrator-provided configuration priority over normal user settings.

The documented limits matter in restrictive desktop deployments:

- `HERMES_MANAGED_DIR` can relocate managed scope; its documentation explicitly says that users controlling this variable can redirect the managed configuration to their own directory.
- Invalid managed configuration is reportedly logged and ignored rather than blocking startup.
- Managed Scope is described as a management boundary, not an unbreakable sandbox.
- According to the documented v1 scope, native Windows managed locations are not yet supported.

The design can be useful in less restrictive environments, but a larger capability surface needs securing, sometimes with additional deployment measures.

Sources: [Hermes Managed Scope](https://hermes-agent.nousresearch.com/docs/zh-Hans/user-guide/managed-scope), [Hermes Tools & Toolsets](https://hermes-agent.nousresearch.com/docs/user-guide/features/tools/), [Hermes Code Execution](https://hermes-agent.nousresearch.com/docs/user-guide/features/code-execution/), [Hermes Provider](https://hermes-agent.nousresearch.com/docs/integrations/providers/).

### goose

goose supports privately hosted and self-hosted OpenAI-compatible LLMs as well as MCP extensions.

Its built-in Developer Extension exposes a `shell` tool that can execute arbitrary system commands with the running user's privileges. The goose documentation itself assigns this tool a high risk level. Tool permissions may be `Always Allow`, `Ask Before` or `Never Allow`; permission modes can change during a session.

For this use case, the decisive question is whether an organization adds a deployment boundary that ordinary users cannot change. In the public documentation considered here, tool permissions are primarily presented as user/session controls.

Sources: [goose Developer Extension](https://github.com/aaif-goose/goose/blob/main/documentation/docs/mcp/developer-mcp.md), [goose Providers](https://github.com/aaif-goose/goose/blob/main/documentation/docs/getting-started/providers.md).

### Continue

Continue supports self-hosted models, OpenAI-compatible endpoints and MCP.

Its tools support `allow`, `ask` and `exclude`, so built-in `Bash` can be hidden. Persistent tool permissions reside in the user file `~/.continue/permissions.yaml`. CLI flags have higher precedence, and the `--auto` and `--readonly` modes override other permission sources.

This suits user control but does not provide the same machine-wide security boundary as `cli-agent`'s `admin_config.toml`.

Sources: [Continue Tool Permissions](https://docs.continue.dev/cli/tool-permissions), [Continue Self-hosted Models](https://docs.continue.dev/guides/how-to-self-host-a-model), [Continue Config Reference](https://docs.continue.dev/reference).

### OpenHands

OpenHands supports local/self-hosted LLMs and MCP. Its Agent SDK includes a persistent terminal tool for shell commands.

OpenHands relies heavily on isolated runtimes and sandboxing for safe execution. That is a valid but different security model. If generic shell execution must be excluded entirely from an approved application, a smaller tool surface is easier to assess.

Sources: [OpenHands Local LLMs](https://github.com/OpenHands/docs/blob/main/openhands/usage/llms/local-llms.mdx), [OpenHands Terminal Tool](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-tools/openhands/tools/terminal/README.md), [OpenHands MCP](https://docs.openhands.dev/sdk/guides/mcp).

## Why a smaller capability surface matters

A universal tool such as a shell is powerful but expands the attack surface: network access, process creation, script execution, package installation and filesystem access can be combined indirectly.

`cli-agent` takes the opposite approach: capabilities should be semantically narrow and individually auditable. A domain MCP can expose exactly the necessary business operations without providing general operating-system control.

The security question changes from:

> What arbitrary actions could an agent execute with the user's privileges?

to:

> Which explicitly defined tools are exposed, what parameters do they accept, and which administrative constraints apply?

## Targeted development and organizational control

A small, open, deliberately limited core also enables targeted additions without depending on a third-party manufacturer's product roadmap.

This does **not** justify deploying unreviewed changes. Managed environments should use a controlled release chain:

```text
Requirement
    -> Implementation and review
    -> Tests / CI
    -> Release artifacts + SBOM + checksums
    -> Organization-specific approval
    -> Pilot / rollout
```

New policy rules, additional validation or narrowly scoped MCP capabilities can then be added while keeping approved revisions reproducible and versioned.

## Summary

`cli-agent` is not designed to outperform established agents in raw autonomy. For this use case, **intentional limitation** is the key feature:

- Custom LLM endpoints, without binding to one model provider.
- MCP as an explicit, auditable capability interface.
- No generic shell or arbitrary code execution in core.
- No persistent cross-project memory in core.
- Machine-wide admin policy that users cannot relax.
- Fail-closed behavior on invalid policy.
- Server identity and tool-contract pinning for permanent auto-approvals.
- Declarative flows with explicit per-step capabilities.
- Small, targeted, evolvable codebase and a controlled release pipeline.

Independent security and operational assessment is still required for a concrete rollout. This document describes the intended design and scope, **not a blanket security certification**.
