"""Backward-compatible alias for cli_agent.context.pdf_text."""
from importlib import import_module as _import_module
import sys as _sys
_sys.modules[__name__] = _import_module(".context.pdf_text", __package__)
