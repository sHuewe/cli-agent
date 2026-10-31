"""Backward-compatible alias for cli_agent.security.mcp_schema_guard."""
from importlib import import_module as _import_module
import sys as _sys
_sys.modules[__name__] = _import_module(".security.mcp_schema_guard", __package__)
if __name__ == "__main__":
    from cli_agent.security.mcp_schema_guard import _main as _entrypoint
    _entrypoint()
