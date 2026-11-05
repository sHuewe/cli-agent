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
- Browser UI translations live in JSON catalogs under
  `src/cli_agent/interfaces/static/i18n/<language>.json`. The server
  discovers valid catalogs dynamically and publishes the supported language
  codes via `/assets/i18n/locales.json`. Add e.g. `fr.json` to enable
  French automatically in the browser's language selector; no code or static
  manifest needs editing. The whole folder is packaged in wheels. The locale
  filename is a lowercase language code (e.g. `de`, `fr`, `pt-br`), and
  the JSON file must be a flat object of string keys and string translations.
  Missing translation keys fall back to `en.json`. English is required.
  The browser detects `navigator.languages` and remembers explicit choices
  in local storage. Switching the UI language never translates agent replies,
  MCP results or technical API keys. Use `data-i18n` for static HTML and
  `t(key, values)` for dynamic messages. Only the loader is in `i18n.js`.
- Prompt translations must preserve security boundaries, exact counts,
  token requirements, tool budgets, evidence and delegation contracts.
  Update behavior-focused tests without deleting their assertions.

## Migration status

The detailed original README is available in [README.de.md](../README.de.md).
Some existing documentation and backend-facing diagnostic strings remain in
German during the staged conversion. Their presence should not be interpreted
as a deliberate long-term exception.
