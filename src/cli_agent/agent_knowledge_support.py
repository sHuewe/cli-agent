"""Backward-compatible alias for cli_agent.context.agent_knowledge_support."""
from importlib import import_module as _import_module
import sys as _sys
_sys.modules[__name__] = _import_module(".context.agent_knowledge_support", __package__)
