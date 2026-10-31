"""Backward-compatible alias for cli_agent.models.model_factory."""
from importlib import import_module as _import_module
import sys as _sys
_sys.modules[__name__] = _import_module(".models.model_factory", __package__)
