# Platform and OS integration

`os_mcp_server.py` exposes workspace operations through the MCP protocol. `os_operations.py` implements sandboxed file access; `execution.py` assembles runtime execution services, `local_commands.py` handles local command contracts, `workspace_access.py` defines access levels, and `artifact_publication.py` manages published artifacts. `logging_setup.py` establishes diagnostics.

File read/write, protected-path validation, symlinks and Windows reparse points are security-sensitive. All entry points must use the same validated workspace and permission contracts.

`workspace_core.py` centralizes policy constants, path guards and shared types, `os_operations.py` retains the `Workspace` public facade and its access checks, and `workspace_io.py` implements file operations against those same checks. Never bypass `Workspace.resolve_path`, protected-path checks or write permissions.
