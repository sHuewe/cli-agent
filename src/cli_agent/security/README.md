# Runtime safety and MCP boundaries

`filesystem_security.py` and `mcp_path_policy.py` prevent unsafe filesystem access; `network_policy.py` validates permitted destinations. `mcp_limits.py`, `mcp_contracts.py`, `mcp_result_artifacts.py` and `mcp_schema_guard.py` enforce tool contracts, payload budgets and schema validation.

The schema worker runs as an isolated process via `python -m cli_agent.security.mcp_schema_guard`. Preserve hard deadlines, restricted worker environment, fork safety and fail-closed handling of malformed responses. Tool auto-approval cannot override this package.
