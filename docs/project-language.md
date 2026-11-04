# Project language policy

English is the canonical language for the CLI Agent codebase.

- All internally authored LLM system prompts, model-facing corrective feedback,
  built-in tool instructions, and prompt examples are written in English.
- The agent must continue to respond in the user's language unless the user
  explicitly requests a different output language.
- Technical identifiers, JSON keys, MCP tool names, and security-policy
  semantics must remain unchanged in translation.
- README, technical guides, comments, and docstrings should use English.
  Historical review artifacts and user-supplied material are retained as
  originally authored.
- UI localization is a separate workstream. Do not translate or replace
  user-visible UI strings while migrating internal prompts; these will use
  a structured i18n catalog in a subsequent change.
- Prompt translations must preserve security boundaries, exact counts,
  token requirements, tool budgets, evidence and delegation contracts.
  Update behavior-focused tests without deleting their assertions.

## Migration status

The detailed original README is available in [README.de.md](../README.de.md).
Some existing documentation and backend-facing diagnostic strings remain in
German during the staged conversion. Their presence should not be interpreted
as a deliberate long-term exception.
