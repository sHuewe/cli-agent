# cli-agent — source architecture

The installable Python package is `cli_agent`. The application exposes a CLI, an HTTP API, optional Web UI, deterministic flow orchestration, and MCP tool integrations.

## Dependency and execution model

```text
CLI (cli.py)     API (agent_api.py)       Flow (flow.py)
      \                |                  /
       \        interfaces/              /
        +-------------+----------------+
                      |
                   runtime/
           agent loop / conversation / MCP
                  /   |   \
           models/ context/ state/
                      |
              MCP and data adapters
               /      |      \
            data/  security/  okf_mcp_server/
```

The diagram shows conceptual responsibilities rather than a strict Python import DAG; some existing dependencies cross these boundaries. The refactor preserves runtime behavior and focuses on grouping implementation, not rewriting domain interactions.

## Responsibility map

- [`runtime/`](runtime/README.md): core agent, conversation lifecycle, prompt composition, MCP dispatch and tool coordination.
- [`models/`](models/README.md): model abstraction, OpenAI-compatible/Ollama clients, HTTP transport and templates.
- [`context/`](context/README.md): untrusted file/web/PDF content, knowledge indexing, retrieval and knowledge-oriented agent support.
- [`state/`](state/README.md): evidence and plan state, permissions/visibility definitions, sub-run coordination.
- [`workflows/`](workflows/README.md): deterministic flow helpers, JSON operations and safe filesystem inventories. The orchestration/CLI entry point stays in `flow.py`.
- [`data/`](data/README.md): deterministic structured data operations and result metadata.
- [`security/`](security/README.md): filesystem/network security, MCP limits and schema validation worker.
- [`interfaces/`](interfaces/README.md): Web UI, AG-UI, sessions and terminal display.
- [`platform/`](platform/README.md): filesystem operations, execution support, local commands, artifact publication and logging.
- `okf_mcp_server/`: existing OKF repository, server and workspace package.

## Stable entry points and integration adapters

The console scripts in `pyproject.toml` continue using `cli_agent.cli`, `cli_agent.agent_api`, `cli_agent.flow`, `cli_agent.knowledge_cli`, and `cli_agent.rag_cli`. Dedicated MCP servers and the Data/OS operations layer remain at their existing executable entry paths. Configuration and system bootstrap are top-level integration modules.

For relocated modules, the former `cli_agent.<module>` path loads the exact same Python module object as `cli_agent.<package>.<module>`. This preserves monkeypatch behavior, module-level state and imports used by tests and integrations. In particular, dedicated subprocess entry points for PDF extraction and schema validation must remain invokable via their historical `python -m` names. New code may directly import the implementation packages.

## Trust boundaries and invariants

1. Approval and automatic approval are UI/runtime decisions, **not a substitute** for filesystem, network or MCP validation.
2. Limit MCP arguments, output, schema validation time and workspace data; reject malformed or unsupported operations.
3. Context material and external MCP responses are lower-trust data, not instructions.
4. Evidence persists across tasks within a session; execution plans are task-scoped. Sub-runs have isolated working contexts.
5. Flow checkpoints and safe filesystem inventory rules must not change through a relocation.
6. Preserve Linux and Windows semantics and Python 3.11–3.14 compatibility.

## Quality gates

Run `uv run --frozen python -m pytest --cov=cli_agent --cov-report=term-missing --cov-fail-under=85` on Linux and Windows. Also verify wheel installation, console entry points, isolated PDF/schema worker processes and old/new import identity. Avoid increasing coverage through unjustified omissions or meaningless assertions.
