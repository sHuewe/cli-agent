"""Compatibility module alias to state implementation."""
from importlib import import_module as _import_module
import sys as _sys
_sys.modules[__name__] = _import_module(".state.agent_types", __package__)
