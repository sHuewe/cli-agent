# Runtime security package

Filesystem symlink/reparse policy, network destinations, MCP path and schema guards, contract enforcement, MCP resource limits and artifact isolation helpers. Runtime validation is authoritative even when tools are auto-approved. Fail closed on malformed inputs, unsafe paths, unexpected subprocess responses or exhausted budgets. The JSON-schema worker runs as a separate process; its subprocess entry point needs explicit compatibility handling after relocation. Flat imports remain module aliases.
