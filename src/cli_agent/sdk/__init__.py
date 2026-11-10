"""Public, embeddable Python API for cli-agent."""

from .builder import AgentBuilder
from cli_agent.runtime.knowledge_trace import KnowledgeHit, KnowledgeSearch, KnowledgeCall, KnowledgeTrace
from .agent import Agent, AgentResult
from .events import AgentEvent, ToolCallRecord, default_event_callback
from .usage import UsageSummary
from .tools import ToolInfo
from .mcp import DirectMcpResult
from .state import SessionState
from .artifacts import AgentArtifact
from .vector_rag import open_vector_rag, VectorRag, VectorRagHit

__all__ = ["Agent", "AgentBuilder", "AgentResult", "AgentEvent", "ToolCallRecord", "default_event_callback", "UsageSummary", "ToolInfo", "SessionState", "AgentArtifact", "DirectMcpResult", "open_vector_rag", "VectorRag", "VectorRagHit", "KnowledgeHit", "KnowledgeSearch", "KnowledgeCall", "KnowledgeTrace"]
