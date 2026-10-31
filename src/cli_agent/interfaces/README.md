# User-facing interfaces

`cli.py` implements the terminal interface and `agent_api.py` the authenticated AG-UI API. `ag_ui_protocol.py`, `ag_ui_frontend_tools.py` and `ag_ui_schema_1_0.json` handle the AG-UI contract. `web_ui.py`, `web_ui_api.py` and `web_ui_sessions.py` provide the Web UI and session handling; `approval_display.py` and `terminal_output.py` render safe user-visible content.

This package is an adapter into `runtime/`; it must not implement alternate authorization logic or assume client-provided approval is sufficient for a tool call. CLI and API entry points are installed through `pyproject.toml`.
