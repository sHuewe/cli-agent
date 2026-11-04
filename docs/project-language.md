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
- Browser UI strings are localized separately in `interfaces/static/i18n.js`.
  The browser supports English and German, detects `navigator.languages`, and
  remembers an explicit selection in browser local storage. Changing the UI
  language does not translate agent responses, MCP results, or technical API keys.
  Keep new browser-owned labels in the catalog and use `data-i18n` for static
  HTML or `t(key, values)` for dynamic messages.
- Prompt translations must preserve security boundaries, exact counts,
  token requirements, tool budgets, evidence and delegation contracts.
  Update behavior-focused tests without deleting their assertions.

## Migration status

The detailed original README is available in [README.de.md](../README.de.md).
Some existing documentation and backend-facing diagnostic strings remain in
German during the staged conversion. Their presence should not be interpreted
as a deliberate long-term exception.
