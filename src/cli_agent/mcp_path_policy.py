"""Backward-compatible alias for cli_agent.security.mcp_path_policy."""
from importlib import import_module as _import_module
import sys as _sys
_sys.modules[__name__] = _import_module(".security.mcp_path_policy", __package__)
