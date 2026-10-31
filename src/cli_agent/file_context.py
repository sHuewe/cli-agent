"""Backward-compatible alias for cli_agent.context.file_context."""
from importlib import import_module as _import_module
import sys as _sys
_sys.modules[__name__] = _import_module(".context.file_context", __package__)
