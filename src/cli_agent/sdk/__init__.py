"""Public, embeddable Python API for cli-agent."""

from .builder import AgentBuilder
from .agent import Agent, AgentResult
from .events import AgentEvent, ToolCallRecord, default_event_callback
from .usage import UsageSummary
from .tools import ToolInfo
from .mcp import DirectMcpResult
from .state import SessionState
from .artifacts import AgentArtifact
from .vector_rag import build_vector_rag, open_vector_rag, VectorRag, VectorRagHit, VectorRagBuildResult

__all__ = ["Agent", "AgentBuilder", "AgentResult", "AgentEvent", "ToolCallRecord", "default_event_callback", "UsageSummary", "ToolInfo", "SessionState", "AgentArtifact", "DirectMcpResult", "build_vector_rag", "open_vector_rag", "VectorRag", "VectorRagHit", "VectorRagBuildResult"]
