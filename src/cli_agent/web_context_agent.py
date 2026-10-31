"""Backward-compatible alias for cli_agent.context.web_context_agent."""
from importlib import import_module as _import_module
import sys as _sys
_sys.modules[__name__] = _import_module(".context.web_context_agent", __package__)
