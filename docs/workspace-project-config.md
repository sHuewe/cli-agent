# Workspace project configuration

cli-agent automatically checks the selected workspace for
`<workspace>/.cli-agent/project.toml`. The file is optional. It supplements
the existing global `config.toml` (or an explicit `--config` file); it does
not replace it.

Configuration precedence, from lowest to highest:

1. Global `config.toml` (or an explicitly selected configuration).
2. Only the fields present in `.cli-agent/project.toml`.
3. Explicit launch arguments (such as `--model`).

Nested TOML tables merge **by key**. For example, if the project sets only
`[model].model`, the global `provider`, `base_url`, `timeout` and
credentials remain in effect. Arrays, including `[[mcp_servers]]`, are
replaced in full when explicitly supplied by the project. Existing validation
and machine-level administrator security policy still apply.

## Project-specific system prompt

The base agent role can be replaced for a workspace without changing the
technical MCP, Evidence, plan, delegation or security instructions.

```text
workspace/
  .cli-agent/
    project.toml
    system.md
    mcp-data-123/
      checkpoint.json
```

```toml
# workspace/.cli-agent/project.toml
[agent]
system_prompt = "system.md"

[model]
model = "project-model"
```

```text
# workspace/.cli-agent/system.md
You are an expert analyst of time-series and industrial automation.
Explain measurements, calculations and uncertainties separately.
```

The `system_prompt` value must be the name of a direct child file in the
workspace's `.cli-agent` directory. Nested, absolute, drive-qualified and
symlinked paths are rejected. Files must be nonempty UTF-8, at most 256 KiB.
An invalid or missing explicitly configured prompt is a startup error.
With no `system_prompt` setting, cli-agent uses its existing built-in
base prompt.

The configured base prompt applies to the main agent and delegated sub-runs.
Knowledge-specific prompts remain unchanged.

### Workspace access and checkpoints

The prompt file and `project.toml` are read by the **host-side configuration
loader**, not by OS or Data MCP file tools. Files directly inside
`.cli-agent/` remain inaccessible through built-in workspace tools, including
when an `allowed_paths` grant happens to cover a parent folder.

This does **not** block existing explicitly permitted subdirectories such
as `.cli-agent/mcp-data-123/`. Data MCP checkpoints and artifacts in those
subdirectories remain readable and writable according to their existing
`allowed_paths` grants. No blanket exclusion of `.cli-agent` is added.

External MCP servers operate under their own capabilities and are not made
private by the built-in file-access rules. Use trusted projects and controlled
external tools when supplying project-level system instructions.

### Startup modes

The same workspace lookup is used by CLI interactive and one-shot sessions,
`cli-agent-api`, the embedded browser UI and Flow steps. In Flow steps,
the project file applies to the selected workspace; any step-specific
configuration is the base before the project override. CLI arguments continue
to win over both files.
