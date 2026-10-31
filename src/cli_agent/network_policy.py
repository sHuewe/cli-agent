"""Backward-compatible alias for cli_agent.security.network_policy."""
from importlib import import_module as _import_module
import sys as _sys
_sys.modules[__name__] = _import_module(".security.network_policy", __package__)
