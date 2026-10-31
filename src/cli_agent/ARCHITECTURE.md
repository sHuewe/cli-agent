# Source architecture

`cli_agent` is an executable application and Python package. Stable entrypoints: `cli.py`, `agent_api.py`, `flow.py`, `knowledge_cli.py`, `rag_cli.py`.

## Execution pipeline

CLI, HTTP API and Web UI establish configuration, sessions and approvals. Agent orchestration (`agent.py`, `agent_loop.py`, `agent_conversation.py`) talks to model providers, retrieves content and handles MCP tool calls. The flow runner coordinates deterministic steps, iteration and checkpoints. Evidence, plan and sub-runs keep scoped execution state.

MCP adapters and server modules expose bounded filesystem, OKF and Data operations. Security validation applies at runtime independently of presentation and auto-approval.

## Package boundaries

- [`data/`](data/README.md): deterministic data transformations, dataframes and structured-result metadata. Data MCP server is an adapter to these operations.
- `okf_mcp_server/`: OKF repository and workspace operations (already modular).
- Remaining modules are scheduled for grouping into agent runtime, model providers, context ingestion, MCP/security and interface packages. Avoid cross-layer coupling.

## Compatibility

Old `cli_agent.<module>` data imports alias the moved implementations using `sys.modules`, rather than forwarding individual symbols. This preserves module identity and monkeypatch behavior. Console script targets, subprocess `-m` modules and configuration file packaging remain stable.

## Tests

Run `uv run --frozen python -m pytest --cov=cli_agent --cov-report=term-missing --cov-fail-under=85` on both Linux and Windows. Wheel installation, import compatibility and entrypoint smoke tests are mandatory before merge.
