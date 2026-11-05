# Enterprise rollout checklist

This checklist applies to `cli-agent` deployments using only internal LLMs. It is an operational approval checklist, **not a replacement for an independent security review**.

Docker Compose and the Python Validator are not part of the core package. If separately maintained servers from `cli-agent-mcp` are installed or connected, each requires its own security assessment and approval.

## Status of technical mitigations addressed during review

The following items document the current hardening status. A checked item indicates that its technical protection is implemented in the core agent and covered by tests/CI. Organizational and operational decisions below remain open for the specific enterprise rollout.

- [x] **Network access limited to explicit destinations.** Model, HTTP MCP and web hosts use separate exact host allowlists. Remote destinations require HTTPS, and URL credentials are rejected. Each web redirect is independently validated; HTTP clients do not implicitly inherit proxies/environment configuration (`trust_env=False`). User configuration cannot override routing/proxy headers such as `Host` or `X-Forwarded-*`.
- [x] **Security policy separated from user configuration.** Network allowlists, external stdio launch profiles, instruction trust and permanent tool auto-approvals belong exclusively in machine-wide `admin_config.toml`. User `config.toml` cannot relax these boundaries.
- [x] **Restrictive defaults without admin policy.** With no `admin_config.toml`, model and HTTP MCP access are localhost-only, web access is disabled, external stdio MCPs are prohibited and no external tool auto-approvals or trusted instructions are granted.
- [x] **Windows admin policy protected against ordinary user changes.** `scripts/setup-admin-config.ps1` requires elevation, protects file and parent directory with ACLs based on well-known SIDs, validates `icacls` results and writes BOM-free UTF-8. Existing policy is replaced only after confirmation or explicit `-Force`.
- [x] **External stdio MCPs individually bound to administrative profiles.** The former global `allow_untrusted_stdio` setting was removed. Each external stdio MCP requires its own `[[mcp.trusted_servers]]` launch profile. User configuration refers to it only by name; command, args and environment come from admin policy. Trusted stdio commands must use absolute executables or `{python}`, not PATH-based executable identities. Runtime placeholders such as `{workspace_directory}` are resolved by the agent.
- [x] **Tool execution guarded by approvals.** External MCP tools require interactive approval by default. Permanent admin auto-approvals are bound to `[[mcp.trusted_servers]]` and a pinned tool contract. HTTP MCP identities require matching name, transport, full normalized endpoint and configured headers; stdio launch identities come entirely from admin profiles. Session approvals remain in-memory and bound to exact tools.
- [x] **MCP instruction trust explicitly limited while retaining functionality.** Built-in MCPs are part of the managed application. External MCP instructions are untrusted by default and supplied as reference data in a transient synthetic `user` message immediately before the real user request. Only an identity-bound administrative `trust_instructions = true` approval places them in the system prompt. The synthetic message is never persisted in conversation history.
- [x] **Built-in OS writes separately protected.** `--with-os-write` enables the capability, but mutating tools still require approval. Trusted-server auto-approvals cannot bypass the built-in rule; explicit session approval is limited to the specific tool.
- [x] **Workspace file access hardened.** Absolute paths, `..`, Windows drives and symlink escapes are rejected. Known secret/credential, `.git`, `.cli-agent` and log paths cannot be read or used as mutation targets. Read sizes are bounded.
- [x] **Absolute workspace paths no longer disclosed to the model.** The system prompt describes relative workspace paths; absolute resolution stays internal to MCP startup.
- [x] **Web, knowledge and explicit file context treated as untrusted input.** Reference data are provided in the same transient synthetic user message. Actual user input is preserved in working context and the synthetic message does not enter `self.history`.
- [x] **Generous emergency safety limits on MCP sizes.** Tool counts, instructions, descriptions, schemas, total metadata and individual results have fixed ceilings well above normal usage, acting as last-resort protection against broken or compromised servers.
- [x] **OKF/Knowledge access contained inside the chosen root.** `[okf].repository` specifies a separate read-only filesystem boundary, potentially outside the project workspace. Relative escapes, absolute follow-up paths, symlink/reparse escapes and hardlink aliases are rejected; read/index limits apply. The agent accepts only repository-offered paths/tools, limits tool/concept calls and validates the final token selection. **Selecting the OKF root remains user configuration and is not constrained by an administrative allowlist.**
- [x] **Logging and debugging data-minimal by default.** Prompt, tool argument, model message, tool result logging and context dumps are opt-in. Approval outputs redact sensitive argument names and large contents. User-configured log paths must remain relative under the cli-agent state directory; absolute paths and traversal are rejected.
- [x] **Optional Docker/code-execution MCPs removed from core.** Docker Compose and Python Validator were moved to `cli-agent-mcp`; Docker daemon and container execution privileges are no longer part of the default core attack surface.
- [x] **Dependencies and tests tightened.** Unused direct `cryptography` dependency was removed. CI verifies `uv.lock` with `uv lock --check` and runs `uv sync --frozen --extra dev` against precisely the locked environment. Security regression tests cover admin policy, networking, MCP approvals, size limits, lifecycle, workspace paths, OKF and tool dispatch. GitHub Actions tests Windows and Ubuntu with Python 3.11.

## Approval prerequisites for a concrete rollout

Source code alone cannot satisfy the following requirements. They must be reviewed and documented for the specific company installation.

- [ ] The model endpoint is operated internally and its exact host is present in machine-wide `model_allowed_hosts`.
- [ ] HTTP MCP and web context are enabled only for necessary, reviewed internal hosts.
- [ ] Only known, audited MCP servers are active.
- [ ] Every external stdio MCP has its own reviewed administrative `[[mcp.trusted_servers]]` launch profile before use. Command, args, environment and runtime placeholders have been explicitly approved.
- [ ] Permanent MCP auto-approvals are limited to necessary external tools and pinned to the reviewed server identity/tool contract. Changes to URL, command, args, headers, explicit environment values, tool descriptions or schemas require reapproval or fall back automatically to interactive approval.
- [ ] `trust_instructions = true` applies only to MCP servers whose operational instructions are intentionally trusted. Leave it off for normal HTTP MCPs; their instructions still enter as untrusted reference data.
- [ ] If OKF is used, note that `[okf].repository` is a user-selected local knowledge source, whose relevant content may be forwarded to the configured LLM. Selecting the root is intentionally not an administrative authorization boundary; the tools remain read-only and contained to that root.
- [ ] Review what confidential data may be stored in each workspace. Technical secret filters provide defense in depth, not a replacement for data classification.

## Host and network hardening

- [ ] The user account and its local permissions meet enterprise policy. Local administrator rights are treated as an explicit trust boundary; machine-wide policy changes require elevation.
- [ ] Outbound firewall policy permits only the internal LLM endpoint and explicitly approved internal MCP/web hosts, as required by the organization.
- [ ] Installation directories and local logs are secured to enterprise standards.
- [ ] OS, Python dependencies and installable artifact are versioned and maintained.
- [ ] Optional packages with extra host permissions or code execution are absent unless required by the approved use case.

## Data and logging

- [ ] Content-bearing diagnostic logging and `dump_llm_context` stay disabled except during explicitly approved diagnostics.
- [ ] The internal LLM endpoint's retention and access rights are reviewed; there is no unintended external telemetry or training redistribution.
- [ ] Before support bundles or backups, inspect/remove `.env`, `.cli-agent`, logs, credentials and keys.
- [ ] Define what data may enter prompts and which data classifications are excluded.

## Review and sign-off

- [x] The automated pytest suite runs in GitHub Actions on Windows and Ubuntu using Python 3.11 and dependencies pinned by `uv.lock`.
- [ ] Ruff, `compileall` and dependency/vulnerability scans pass for the final approved revision or are incorporated into the release process. Locking improves reproducibility but does not replace vulnerability assessment.
- [x] Hardening and regression tests consider prompt injection/untrusted context, MCP exfiltration, external stdio servers, path/symlink access, invalid tool calls, oversized MCP results and lifecycle failures.
- [ ] Independent security review is completed or formally accepted for production rollout.
- [ ] Responsible owner, purpose, allowed endpoints, active servers and approval expiration are recorded.
- [ ] Rollback is tested: stop the agent, disable MCP servers, restore admin/user configuration and rotate tokens as necessary.

## Optional MCP extensions

Extra MCP packages are not automatically covered by general `cli-agent` approval. Docker daemon access, container execution and other host-level capabilities require separate assessment only when the relevant external MCP package is actually installed and activated. External stdio extensions additionally require their own administrative launch profile.

## Minimum approval decision

Core technical findings have been addressed or documented with mitigations in [security.md](security.md). An internal pilot or organization-wide rollout additionally requires completing the remaining operational/organizational checks and obtaining documented approval from the responsible security owner.
