"""Compatibility module alias to workflows implementation."""
from importlib import import_module as _import_module
import sys as _sys
_sys.modules[__name__] = _import_module(".workflows.flow_json_grouping", __package__)
