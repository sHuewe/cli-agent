"""Backward-compatible alias for cli_agent.context.knowledge_vector."""
from importlib import import_module as _import_module
import sys as _sys
_sys.modules[__name__] = _import_module(".context.knowledge_vector", __package__)
