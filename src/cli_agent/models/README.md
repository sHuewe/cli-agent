# Models package

Model abstraction, provider factory, Ollama and OpenAI-compatible clients, HTTP helpers and prompt templates. Orchestration calls the providers and owns the lifecycle and context; model clients must not bypass MCP permissions or initiate workspace writes. Legacy `cli_agent.<module>` imports are preserved as module-identity aliases.
