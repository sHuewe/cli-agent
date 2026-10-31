# cli-agent source architecture

This document describes the implementation, package boundaries, runtime flow and trust model. The code lives under `cli_agent` and follows **canonical package imports only**. The former flat modules have been removed, not retained as compatibility shims.

## Application flow

```text
interfaces/cli.py, interfaces/agent_api.py
             |
        runtime/agent.py
             |
    runtime/agent_loop.py ----- models/ (OpenAI/Ollama)
          |        |
       context/    state/ (evidence, plan, sub-runs)
          |
      MCP tools ---------------------+
        |                            |
   security/ policies         data/, platform/,
                              okf_mcp_server/
```

`workflows/flow.py` provides an additional entry point for repeatable workflows and checkpoint/resume; it composes the same runtime and adapters. `settings/` defines user and admin configuration. This is a conceptual architecture, not a guarantee that imports form a strict acyclic graph.

## Source packages

- [`runtime/`](runtime/README.md): agent execution, conversations, tool dispatch and model loop
- [`models/`](models/README.md): LLM clients, HTTP transport and templates
- [`context/`](context/README.md): lower-trust file/web/PDF content, vector knowledge and knowledge CLIs
- [`state/`](state/README.md): session evidence, task plans, sub-runs and related types
- [`workflows/`](workflows/README.md): Flow CLI, checkpointing, JSON transforms and inventory helpers
- [`data/`](data/README.md): Data MCP server, DataFrame operations and bounded result metadata
- [`security/`](security/README.md): network/filesystem constraints, MCP schema worker, limits and contracts
- [`platform/`](platform/README.md): OS MCP server, workspace I/O, execution services and logging
- [`interfaces/`](interfaces/README.md): CLI, AG-UI API, Web UI and terminal-facing adapters
- [`settings/`](settings/README.md): validated user/admin settings and bundled config template
- `okf_mcp_server/`: OKF repository, workspace and MCP server implementation

## Executable entry points

`pyproject.toml` maps the console scripts to `cli_agent.interfaces.cli:main`, `cli_agent.interfaces.agent_api:main`, `cli_agent.workflows.flow:main`, `cli_agent.context.knowledge_cli:main` and `cli_agent.context.rag_cli:main`.

The two isolated parser/validator workers are called via `python -m cli_agent.context.pdf_text` and `python -m cli_agent.security.mcp_schema_guard`. Existing Data/OS/OKF MCP launchers must use their canonical modules too: `cli_agent.data.data_mcp_server`, `cli_agent.platform.os_mcp_server` and `cli_agent.okf_mcp_server.server`.

## Trust boundaries

1. Every MCP operation is checked at runtime; interface-level approvals never supersede filesystem, URL, schema or data-size policies.
2. Untrusted web, document, MCP and model output remains data, not system instructions.
3. Evidence persists within a session; plans are reset per task; sub-runs isolate their working context.
4. File access is anchored to validated workspace paths. Preserve symlink/reparse protections, network allowlists and subprocess budgets.
5. Flow retries, checkpoint semantics, cancellation and tool lifecycle are behavioral contracts, independent of module placement.

## Verification

Run `uv run --frozen python -m pytest --cov=cli_agent --cov-report=term-missing --cov-fail-under=85` on Linux and Windows; use the CI compatibility matrix for Python 3.12–3.14. Build/install the wheel and validate script entry points plus subprocess workers. The structural regression test checks that the Python package root contains only `__init__.py`.
