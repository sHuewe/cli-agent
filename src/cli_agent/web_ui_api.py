"""Compatibility module alias to interfaces implementation."""
from importlib import import_module as _import_module
import sys as _sys
_sys.modules[__name__] = _import_module(".interfaces.web_ui_api", __package__)
