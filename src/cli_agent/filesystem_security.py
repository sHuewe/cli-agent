"""Backward-compatible alias for cli_agent.security.filesystem_security."""
from importlib import import_module as _import_module
import sys as _sys
_sys.modules[__name__] = _import_module(".security.filesystem_security", __package__)
