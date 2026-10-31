"""Backward-compatible alias for cli_agent.models.openai_client."""
from importlib import import_module as _import_module
import sys as _sys
_sys.modules[__name__] = _import_module(".models.openai_client", __package__)
