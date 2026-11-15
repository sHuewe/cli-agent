"""Deterministic joins for section-level OKF coverage and theme registries.

All model-authored data is untrusted. Missing or duplicate source IDs are errors,
never silently converted into success statuses.
"""
from __future__ import annotations

from typing import Any, Callable

from cli_agent.workflows.flow_json_grouping import group_assigned_items


def _obj(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a JSON object.")
    return value


def _arr(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a JSON array.")
    return value


def _str(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string.")
    return value


def group_consolidated_knowledge(
    *,
    sections: Any,
    scans: Any,
    consolidated: Any,
    max_concept_bytes: int,
    serializer: Callable[[Any], str],
) -> dict[str, Any]:
    """Map ALL section knowledge items to merged document concepts without LLMs.

    The section analyzer assigns each knowledge item a local semantic topic_key.
    Section outlines preserve the key; doctrine consolidation retains it in
    merged_from. This operation verifies the one-to-one mapping and forwards
    the original, source-grounded items to the existing group_items splitter.
    """
    manifest = _obj(sections, "sections")
    plan = _obj(consolidated, "consolidated")
    folder = _obj(plan.get("folder"), "consolidated.folder")
    doc_id = _str(folder.get("id"), "folder.id")
    source = _str(plan.get("source_path"), "consolidated.source_path")
    if plan.get("status") != "ok":
        raise ValueError("Cannot group incomplete consolidated topics.")
    if manifest.get("source_path") != source:
        raise ValueError("Section manifest does not match consolidated source.")
    mode = manifest.get("mode")
    if mode == "direct":
        expected = {"direct"}
    elif mode == "sections":
        expected = {
            _str(_obj(s, "sections.sections[]").get("id"), "section.id")
            for s in _arr(manifest.get("sections"), "sections.sections")
        }
        if len(expected) != len(manifest["sections"]) or not expected:
            raise ValueError("Missing or duplicate section IDs.")
    else:
        raise ValueError("Unsupported section mode.")

    aliases: dict[str, str] = {}
    topics: list[dict[str, Any]] = []
    for concept in _arr(plan.get("concepts"), "consolidated.concepts"):
        c = _obj(concept, "consolidated.concepts[]")
        cid = _str(c.get("id"), "concept.id")
        merged_from = _arr(c.get("merged_from", [cid]), "concept.merged_from")
        if not merged_from:
            raise ValueError("Consolidated concept has no original topic IDs.")
        for raw_key in merged_from:
            key = _str(raw_key, "merged_from[]")
            if key in aliases:
                raise ValueError(f"Duplicate canonical topic mapping for {key}.")
            aliases[key] = cid
        topics.append({
            field: c[field]
            for field in ("id", "path", "type", "title", "description", "tags")
            if field in c
        })
    if not topics:
        raise ValueError("Consolidated document has no topics.")

    routes = []
    assignments = []
    seen_scans: set[str] = set()
    referenced_keys: set[str] = set()
    for record in _arr(scans, "scans"):
        scan = _obj(record, "scans[]")
        sid = _str(scan.get("section_id"), "scan.section_id")
        if sid not in expected or sid in seen_scans:
            raise ValueError(f"Unexpected or duplicate scanned section {sid}.")
        seen_scans.add(sid)
        if (scan.get("status") != "ok"
                or scan.get("source_path") != source
                or scan.get("document_id") != doc_id
                or scan.get("chunk_id") != sid
                or scan.get("folder_id") != doc_id):
            raise ValueError(f"Invalid/incomplete scan {sid}.")
        if scan.get("warnings"):
            raise ValueError(f"Unresolved scan warnings for section {sid}.")
        if mode == "direct":
            first, last = 1, manifest.get("total_lines")
        else:
            current = next(section for section in manifest["sections"] if section["id"] == sid)
            first, last = current["start_line"], current["end_line"]
        if (scan.get("start_line") != first or scan.get("end_line") != last):
            raise ValueError(f"Section {sid} has inconsistent source line ranges.")
        candidates = _arr(scan.get("candidate_concepts"), "candidate_concepts")
        local_keys = set()
        for candidate in candidates:
            key = _str(_obj(candidate, "candidate").get("topic_key"), "candidate.topic_key")
            if key in local_keys:
                raise ValueError(f"Duplicate topic_key in section {sid}.")
            local_keys.add(key)
            if key not in aliases:
                raise ValueError(f"Unrepresented source topic_key {key}.")
        route_items = []
        for raw_item in _arr(scan.get("knowledge_items"), "knowledge_items"):
            item = _obj(raw_item, "knowledge_item")
            key = _str(item.get("topic_key"), "knowledge_item.topic_key")
            if key not in local_keys:
                raise ValueError(f"Knowledge item with unknown local topic_key {key}.")
            referenced_keys.add(key)
            for raw_evidence in _arr(item.get("evidence_ranges"), "evidence_ranges"):
                evidence = _obj(raw_evidence, "evidence")
                lo, hi = evidence.get("start_line"), evidence.get("end_line")
                if (evidence.get("doctrine") != source
                        or type(lo) is not int or type(hi) is not int
                        or not first <= lo <= hi <= last):
                    raise ValueError(f"Knowledge item references invalid source range in {sid}.")
            clean_item = {k: v for k, v in item.items() if k != "topic_key"}
            route_items.append(clean_item)
            item_id = _str(item.get("id"), "knowledge_item.id")
            assignments.append({"item_id": f"{doc_id}:{item_id}", "topic_id": aliases[key]})
        routes.append({
            "document_id": doc_id, "chunk_id": sid, "folder_id": doc_id,
            "candidate_concepts": candidates, "knowledge_items": route_items,
            "warnings": [],
        })
    if seen_scans != expected:
        raise ValueError("Missing section scans: " + ", ".join(sorted(expected - seen_scans)[:10]))
    if not referenced_keys:
        raise ValueError("Scanned document has no source-grounded knowledge items.")
    # The old group_items implementation checks every source item is assigned
    # exactly once and automatically splits concepts when max_concept_bytes is reached.
    result = group_assigned_items(
        routes=routes,
        assignments=[{"assignments": assignments}],
        topic_plan={"folder": folder, "folders": plan.get("folders", []), "topics": topics},
        max_concept_bytes=max_concept_bytes,
        serializer=serializer,
    )
    result["source_path"] = source
    result["source_slug"] = doc_id
    return result


def validate_theme_registry(*, batch: Any, registry: Any) -> dict[str, Any]:
    """Fail closed if any cross-document candidate disappears in registry LLM output.

    References use iNNNN-gNNNN (1-based index within batch.items, group index
    within each item). Eligible multi-source groups must be covered by exactly
    one theme; all other groups must be explicitly excluded.
    """
    source = _obj(batch, "batch")
    result = _obj(registry, "registry")
    batch_id = _str(source.get("id"), "batch.id")
    groups: dict[str, tuple[bool, set[tuple[str, str]]]] = {}
    incomplete = False
    for item_index, raw_item in enumerate(_arr(source.get("items"), "batch.items"), 1):
        item = _obj(raw_item, "batch.items[]")
        if item.get("status") != "ok" or item.get("warnings"):
            incomplete = True
        for group_index, raw_group in enumerate(_arr(item.get("groups"), "item.groups"), 1):
            group = _obj(raw_group, "group")
            ref = f"i{item_index:04d}-g{group_index:04d}"
            members: set[tuple[str, str]] = set()
            for entry in _arr(group.get("members"), "group.members"):
                m = _obj(entry, "group.member")
                members.add((_str(m.get("source_slug"), "source_slug"),
                             _str(m.get("path"), "member.path")))
            if not members:
                raise ValueError(f"Empty registry group {ref}.")
            groups[ref] = (len({slug for slug, _ in members}) >= 2, members)
    found: set[str] = set()
    themes = _arr(result.get("themes"), "registry.themes")
    theme_ids: set[str] = set()
    for theme in themes:
        obj = _obj(theme, "theme")
        identifier = _str(obj.get("id"), "theme.id")
        if not identifier.startswith(batch_id + "-") or identifier in theme_ids:
            raise ValueError("Duplicate or invalid theme identifier.")
        theme_ids.add(identifier)
        if obj.get("path") != f"themen/{identifier}.md":
            raise ValueError("Theme has incorrect relative path.")
        refs = _arr(obj.get("group_refs"), "theme.group_refs")
        if not refs:
            raise ValueError("Theme does not reference original groups.")
        theme_members = {
            (_str(_obj(m, "member").get("source_slug"), "source_slug"),
             _str(m.get("path"), "member.path"))
            for m in _arr(obj.get("members"), "theme.members")
        }
        required: set[tuple[str, str]] = set()
        for ref in refs:
            if ref in found or ref not in groups or not groups[ref][0]:
                raise ValueError(f"Duplicate, unknown or ineligible group reference: {ref}.")
            found.add(ref)
            required.update(groups[ref][1])
        if theme_members != required:
            raise ValueError(f"Theme {identifier} omitted or invented concept members.")
    excluded = _arr(result.get("excluded_group_refs"), "registry.excluded_group_refs")
    for ref in excluded:
        if ref in found or ref not in groups or groups[ref][0]:
            raise ValueError(f"Eligible or duplicate group excluded: {ref}.")
        found.add(ref)
    if found != set(groups):
        raise ValueError("Registry missing input groups: " + ", ".join(sorted(set(groups) - found)[:10]))
    upstream_status = result.get("status")
    if upstream_status not in ("ok", "needs_attention"):
        raise ValueError("Registry status must be ok or needs_attention.")
    warnings = _arr(result.get("warnings"), "registry.warnings")
    if incomplete and upstream_status == "ok":
        raise ValueError("Registry converted upstream needs_attention into ok.")
    return {
        "status": "needs_attention" if incomplete or upstream_status != "ok" or warnings else "ok",
        "batch_id": batch_id, "themes": themes,
        "input_group_count": len(groups), "eligible_group_count": sum(v[0] for v in groups.values()),
        "excluded_group_refs": excluded, "warnings": warnings,
    }
