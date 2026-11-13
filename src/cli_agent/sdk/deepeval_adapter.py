"""Optional DeepEval tracing adapter for the public cli-agent SDK.

Importing cli_agent.sdk never requires DeepEval; only calling
DeepEvalTarget.ask needs the optional third-party package.
"""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any, Callable, Mapping


def _verified_retrieval_context(trace: Any) -> list[str] | None:
    """Only complete host-verified Knowledge selected text, never golden quotes."""
    if is_dataclass(trace):
        trace = asdict(trace)
    if not isinstance(trace, dict) or trace.get("truncated") is True:
        return None
    if trace.get("content_included") is not True:
        return None
    selected = trace.get("selected")
    if not isinstance(selected, (list, tuple)) or not selected:
        return None
    contents = [
        item.get("content") if isinstance(item, dict) else None
        for item in selected
    ]
    if any(not isinstance(text, str) or not text.strip() for text in contents):
        return None
    return contents


class DeepEvalTarget:
    """Thin observed callable; new SDK Agent session for each uncached question.

    Use inside DeepEval's native EvaluationDataset.evals_iterator(). A passed
    expected_output is used ONLY to annotate the judge's trace and is NEVER
    forwarded to cli-agent. The optional cached_response replays an earlier
    SDK answer into a new trace without calling the underlying agent again.

    The caller owns response persistence and freshness decisions. DeepEval
    owns metric execution, metric caching, and test-run reporting.
    """

    def __init__(self, create_agent: Callable[[], Any]) -> None:
        if not callable(create_agent):
            raise TypeError("create_agent must be callable")
        self._create_agent = create_agent

    async def ask(
        self,
        question: str,
        *,
        expected_output: str,
        cached_response: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(question, str) or not question.strip():
            raise ValueError("question must be nonempty")
        if not isinstance(expected_output, str) or not expected_output.strip():
            raise ValueError("expected_output must be nonempty")
        if cached_response is not None:
            if not isinstance(cached_response, Mapping):
                raise TypeError("cached_response must be a response mapping")
            actual = cached_response.get("actual_output")
            trace = cached_response.get("knowledge_trace")
            if not isinstance(actual, str) or not actual.strip():
                raise ValueError("cached_response requires nonempty actual_output")
            if trace is not None and not isinstance(trace, dict):
                raise ValueError("cached_response knowledge_trace must be a dict")
            if trace is not None and not isinstance(trace.get("selected"), list):
                raise ValueError("cached_response knowledge_trace requires selected list")

        try:
            from deepeval.tracing import observe, update_current_trace
        except ImportError as exc:
            raise ImportError(
                "DeepEval integration requires optional package 'deepeval>=4,<5'."
            ) from exc

        @observe(type="agent")
        async def invoke(prompt: str) -> dict[str, Any]:
            if cached_response is None:
                # The builder is configured by the caller. Each execution gets
                # an independent agent/history/tools/approvals lifecycle.
                agent = self._create_agent()
                async with agent:
                    result = await agent.ask(prompt)  # ONLY the question
                answer = result.answer
                trace = result.knowledge_trace
                if is_dataclass(trace):
                    trace = asdict(trace)
                if trace is not None and not isinstance(trace, dict):
                    raise ValueError("Invalid SDK KnowledgeTrace type")
                response = {
                    "actual_output": answer,
                    "knowledge_trace": trace,
                }
            else:
                response = {
                    "actual_output": cached_response["actual_output"],
                    "knowledge_trace": cached_response.get("knowledge_trace"),
                }

            if (not isinstance(response["actual_output"], str)
                    or not response["actual_output"].strip()):
                raise ValueError("cli-agent returned an empty answer")

            selected = _verified_retrieval_context(response["knowledge_trace"])
            attrs: dict[str, Any] = {
                "input": prompt,
                "output": response["actual_output"],
                "expected_output": expected_output,
                "metadata": {"cli_agent_response_cached": cached_response is not None},
            }
            if selected:
                attrs["retrieval_context"] = selected
            update_current_trace(**attrs)
            return response

        return await invoke(question)
