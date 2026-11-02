# Python SDK (experimental)

The SDK is part of the regular `cli-agent` package, not a separate distribution.
The CLI programs `cli-agent`, `cli-agent-api`, and `cli-agent-flow` still
load the mandatory system-wide admin policy. SDK policy selection never changes
these programs.

## Jupyter / asynchronous applications

```python
from cli_agent.sdk import AgentBuilder

agent = (
    AgentBuilder()
    .with_admin_config(
        model_allowed_hosts=["localhost", "api.example.com"],
        mcp_allowed_hosts=["localhost"],
    )
    .with_project_config(
        workspace=".",
        model="qwen3.5:9b",
        provider="ollama",
        base_url="http://localhost:11434",
        workspace_access="read",
        with_data=True,
    )
    .build()
)

await agent.start()
try:
    first = await agent.ask("Analyze the available data.")
    second = await agent.ask("Check your previous analysis.")
    print(second.answer)
finally:
    await agent.close()
```

`async with agent:` is also supported. `ask` returns an `AgentResult`
with an `answer` string. The session retains conversation history and evidence
between calls. MCP connections live on a single worker task even when notebook
cells submit their prompts in separate tasks.

## Separate configurations

- No `with_admin_config` call: strict `AdminConfig()` defaults; the
  machine-wide policy is **not** read.
- `with_admin_config()`: explicitly load the system-wide admin policy.
- `with_admin_config(path="...")`: load that exact policy file; nonexistent
  explicit files are rejected.
- `with_admin_config(model_allowed_hosts=[...], ...)`: build a policy from
  strict defaults without reading any file.
- `with_admin_config(path="...", model_allowed_hosts=[...])`: load then
  replace the named settings.
- `with_project_config()`: load the regular default project config.
- `with_project_config(path="...")`: load a specific project config.
- `with_project_config(model="...", workspace="...", ...)`: start from
  project defaults and override the given settings without reading a file.

Host-list overrides **replace** the previous list. For richer policies,
`model_credentials`, `trusted_servers`, and `web_providers` accept the
respective existing configuration dataclasses.

The SDK does not sandbox arbitrary code running in the same Python interpreter.
It enforces the selected runtime policy against actions of the agent/LLM.
External tool registration from Python is reserved for a subsequent change.
