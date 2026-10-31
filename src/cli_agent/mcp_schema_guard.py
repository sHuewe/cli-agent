"""Compatibility module and subprocess entry point for MCP schema worker."""
from importlib import import_module as _import_module
import sys as _sys
if __name__ == "__main__":
    from cli_agent.security.mcp_schema_guard import _worker_main
    raise SystemExit(_worker_main())
_sys.modules[__name__] = _import_module(".security.mcp_schema_guard", __package__)
