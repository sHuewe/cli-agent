# User-facing interfaces

`cli.py` implements the terminal interface and `agent_api.py` the authenticated AG-UI API. `ag_ui_protocol.py`, `ag_ui_frontend_tools.py` and `ag_ui_schema_1_0.json` handle the AG-UI contract. `web_ui.py`, `web_ui_api.py` and `web_ui_sessions.py` provide the Web UI and session handling; `approval_display.py` and `terminal_output.py` render safe user-visible content.

This package is an adapter into `runtime/`; it must not implement alternate authorization logic or assume client-provided approval is sufficient for a tool call. CLI and API entry points are installed through `pyproject.toml`.

`agent_api_sessions.py` owns `AgentRun`, `AgentSession`, `AgentApiService` and their concurrency/worker lifecycle. `agent_api.py` is the public API entrypoint and owns HTTP/AG-UI route construction. The API JavaScript is a package resource at `static/app.js`, exposed through `web_ui_api.API_APP_JS` and the unchanged `/assets/app.js` endpoint.

HTTP route construction is split between `api_routes.py` (session, AG-UI streaming and context) and `browser_routes.py` (browser resources, tab leases, browser commands); `agent_api.py` retains the host/authentication middleware, request size checks, ASGI assembly and CLI entrypoint. Handler factories receive explicit service, browser state and request validators rather than relying on mutable module globals.
