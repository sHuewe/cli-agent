"""Public, embeddable Python API for cli-agent."""

from .builder import AgentBuilder
from .agent import Agent, AgentResult
from .events import AgentEvent, ToolCallRecord, default_event_callback
from .usage import UsageSummary
from .tools import ToolInfo

__all__ = ["Agent", "AgentBuilder", "AgentResult", "AgentEvent", "ToolCallRecord", "default_event_callback", "UsageSummary", "ToolInfo"]
