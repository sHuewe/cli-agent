"""Validate each scan against its planned section before reconciling routing folder IDs.

The scan model can provide a noncanonical folder_id. This is metadata, not
knowledge content. Only the *in-memory* group input is normalized. The
original scan and assignment checkpoints must remain byte-for-byte unchanged.
"""
from __future__ import annotations

from typing import Any


def canonicalize_group_routes(
    routes: Any, sections: Any, topics: Any,
) -> list[dict[str, Any]]:
    if not isinstance(routes, list):
        raise ValueError("Group routes must be a list.")
    if not isinstance(sections, dict) or not isinstance(sections.get("sections"), list):
        raise ValueError("Authoritative sections plan is missing.")
    if not isinstance(topics, dict) or not isinstance(topics.get("folder"), dict):
        raise ValueError("Authoritative topic folder is missing.")
    folder_id = topics["folder"].get("id")
    folder_path = topics["folder"].get("path")
    source_path = sections.get("source_path")
    if (not isinstance(folder_id, str) or not folder_id
            or folder_path != f"doctrines/{folder_id}"
            or not isinstance(source_path, str) or not source_path):
        raise ValueError("Authoritative folder or source identity is invalid.")
    # These source IDs also construct the global IDs used in assign.json.
    expected: dict[str, dict[str, Any]] = {}
    for section in sections["sections"]:
        if not isinstance(section, dict):
            raise ValueError("Malformed planned section.")
        sid = section.get("id")
        if not isinstance(sid, str) or not sid or sid in expected:
            raise ValueError("Duplicate or invalid planned section ID.")
        start, end = section.get("start_line"), section.get("end_line")
        if type(start) is not int or type(end) is not int or not 1 <= start <= end:
            raise ValueError("Invalid planned section range.")
        expected[sid] = section
    if not expected or len(routes) != len(expected):
        raise ValueError("Scan count does not match authoritative sections.")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for route in routes:
        if not isinstance(route, dict):
            raise ValueError("Malformed scan route.")
        sid = route.get("section_id")
        if not isinstance(sid, str) or sid not in expected or sid in seen:
            raise ValueError("Unknown or duplicate scanned section.")
        seen.add(sid)
        planned = expected[sid]
        for key, expected_value in (
            ("source_path", source_path),
            ("start_line", planned["start_line"]),
            ("end_line", planned["end_line"]),
            ("chunk_id", sid),
            ("document_id", folder_id),
        ):
            if route.get(key) != expected_value:
                raise ValueError(
                    f"Scan {sid}: {key} does not match the section plan "
                    "or canonical document identity."
                )
        if route.get("status") != "ok":
            raise ValueError(f"Scan {sid} is not complete.")
        # All content is passed through without edits. Only folder_id
        # changes, and only once every independently planned field matches.
        result.append({**route, "folder_id": folder_id})
    return result
