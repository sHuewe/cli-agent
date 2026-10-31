"""Backward-compatible alias for cli_agent.data.data_markdown."""
from importlib import import_module as _import_module
import sys as _sys
_sys.modules[__name__] = _import_module(".data.data_markdown", __package__)
