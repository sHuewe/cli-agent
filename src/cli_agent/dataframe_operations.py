"""Backward-compatible alias for cli_agent.data.dataframe_operations."""
from importlib import import_module as _import_module
import sys as _sys
_sys.modules[__name__] = _import_module(".data.dataframe_operations", __package__)
