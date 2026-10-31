"""Compatibility module alias to platform implementation."""
from importlib import import_module as _import_module
import sys as _sys
_sys.modules[__name__] = _import_module(".platform.os_operations", __package__)
