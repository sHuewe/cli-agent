# Agent runtime package

Core agent execution: prompt composition, conversation lifecycle, model-loop orchestration, MCP tool coordination and response processing. It consumes model providers and context adapters, delegates evidence/sub-runs to `state/`, and applies MCP safety policy before executing tools. CLI, HTTP API, and Web UI are adapters into this runtime. Keep entrypoint-specific authorization outside the model response, with runtime checks authoritative. Flat imports remain identical module aliases, not copies.
