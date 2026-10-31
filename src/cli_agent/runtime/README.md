# Agent orchestration

`agent.py` builds the agent, `agent_conversation.py` manages conversation messages, `agent_loop.py` coordinates model turns, `agent_mcp.py` manages MCP server/tool lifecycle, `agent_tool_calls.py` executes tool requests and `agent_prompts.py` supplies prompt content.

The runtime integrates `models/`, `context/`, `state/` and `security/`. It enforces MCP authorization irrespective of CLI, HTTP or Flow callers. Keep tool side effects, evidence/plan updates, token budgets and sub-run isolation explicit and testable.

`context_dumps.py` now owns the secure, bounded context-dump machinery through `ContextDumpMixin`. `CliAgent` remains responsible for orchestration and reuses that implementation without duplicating its symlink, hardlink and Windows filename checks.
