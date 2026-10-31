"""Backward-compatible alias for cli_agent.data.data_result_metadata."""
from importlib import import_module as _import_module
import sys as _sys
_sys.modules[__name__] = _import_module(".data.data_result_metadata", __package__)
