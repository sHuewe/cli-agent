from __future__ import annotations

import json
from typing import Any

# These limits are intentionally generous. They are last-resort safety bounds
# against broken or compromised MCP servers, not normal application quotas.
MAX_MCP_TOOLS_PER_SERVER = 1_024
MAX_MCP_INSTRUCTIONS_CHARS = 2_000_000
MAX_MCP_TOOL_DESCRIPTION_CHARS = 250_000
MAX_MCP_TOOL_SCHEMA_CHARS = 2_000_000
MAX_MCP_TOTAL_TOOL_METADATA_CHARS = 20_000_000
MAX_MCP_TOOL_RESULT_CHARS = 10_000_000


def validate_mcp_server_metadata(
    *,
    server_name: str,
    instructions: str | None,
    tools: list[Any],
) -> None:
    if instructions is not None and len(instructions) > MAX_MCP_INSTRUCTIONS_CHARS:
        raise RuntimeError(
            f"MCP-Server {server_name!r} liefert zu große Instructions "
            f"({len(instructions)} > {MAX_MCP_INSTRUCTIONS_CHARS} Zeichen)."
        )
    if len(tools) > MAX_MCP_TOOLS_PER_SERVER:
        raise RuntimeError(
            f"MCP-Server {server_name!r} bietet zu viele Tools an "
            f"({len(tools)} > {MAX_MCP_TOOLS_PER_SERVER})."
        )

    total_metadata_chars = 0
    for tool in tools:
        tool_name = str(getattr(tool, "name", "<unbekannt>"))
        description = str(getattr(tool, "description", None) or "")
        if len(description) > MAX_MCP_TOOL_DESCRIPTION_CHARS:
            raise RuntimeError(
                f"MCP-Tool {server_name}__{tool_name} hat eine zu große Beschreibung "
                f"({len(description)} > {MAX_MCP_TOOL_DESCRIPTION_CHARS} Zeichen)."
            )
        try:
            schema_text = json.dumps(
                getattr(tool, "inputSchema", {}),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"MCP-Tool {server_name}__{tool_name} liefert kein gültig serialisierbares Schema."
            ) from exc
        if len(schema_text) > MAX_MCP_TOOL_SCHEMA_CHARS:
            raise RuntimeError(
                f"MCP-Tool {server_name}__{tool_name} hat ein zu großes Schema "
                f"({len(schema_text)} > {MAX_MCP_TOOL_SCHEMA_CHARS} Zeichen)."
            )
        total_metadata_chars += len(description) + len(schema_text)
        if total_metadata_chars > MAX_MCP_TOTAL_TOOL_METADATA_CHARS:
            raise RuntimeError(
                f"MCP-Server {server_name!r} liefert insgesamt zu viele Tool-Metadaten "
                f"({total_metadata_chars} > {MAX_MCP_TOTAL_TOOL_METADATA_CHARS} Zeichen)."
            )


def enforce_mcp_tool_result_limit(text: str) -> str:
    if len(text) > MAX_MCP_TOOL_RESULT_CHARS:
        raise RuntimeError(
            "MCP-Tool-Ergebnis überschreitet das Sicherheitslimit "
            f"({len(text)} > {MAX_MCP_TOOL_RESULT_CHARS} Zeichen)."
        )
    return text
