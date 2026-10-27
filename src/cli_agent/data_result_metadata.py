"""Automatic MCP result metadata for bounded, session-scoped data artifacts."""

from __future__ import annotations

from mcp.types import CallToolResult, TextContent

from .mcp_result_artifacts import METADATA_FOR_LLM_KEY, ONLY_METADATA_TO_LLM_KEY


# Small complete results remain directly visible, while every result also
# receives a session path so subsequent file-based tools can reuse it.
INLINE_RESULT_BYTES = 5_000


def markdown_result(
    text: str, columns: list[str], row_count: int, *, operation: str
) -> CallToolResult:
    metadata: dict[str, object] = {
        "operation": operation,
        "row_count": row_count,
        "columns": columns,
        "format": "markdown",
    }
    if len(text.encode("utf-8")) <= INLINE_RESULT_BYTES:
        metadata["result"] = text
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        **{
            "_meta": {
                ONLY_METADATA_TO_LLM_KEY: True,
                METADATA_FOR_LLM_KEY: metadata,
            }
        },
    )
