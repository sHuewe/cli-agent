"""Deterministic joins for section-level OKF coverage and theme registries.

All model-authored data is untrusted. Missing or duplicate source IDs are errors,
never silently converted into success statuses.
"""
from __future__ import annotations

from typing import Any, Callable
import re

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
        checks = _obj(scan.get("scan_checks"), "scan_checks")
        if (checks.get("normative_sweep") != "completed"
                or checks.get("descriptive_sweep") != "completed"):
            raise ValueError(f"Section {sid} is not fully scanned.")
        route_items = []
        for raw_item in _arr(scan.get("knowledge_items"), "knowledge_items"):
            item = _obj(raw_item, "knowledge_item")
            key = _str(item.get("topic_key"), "knowledge_item.topic_key")
            if key not in local_keys:
                raise ValueError(f"Knowledge item with unknown local topic_key {key}.")
            referenced_keys.add(key)
            evidence_ranges = _arr(item.get("evidence_ranges"), "evidence_ranges")
            if not evidence_ranges:
                raise ValueError(f"Knowledge item in {sid} has no supporting source evidence.")
            for raw_evidence in evidence_ranges:
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

    def member_ref(raw: Any) -> tuple[str, str]:
        member = _obj(raw, "group member")
        slug = _str(member.get("source_slug"), "member.source_slug")
        path = _str(member.get("path"), "member.path")
        if (not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug)
                or not re.fullmatch(
                    rf"doctrines/{re.escape(slug)}/"
                    r"(?:[a-z0-9]+(?:-[a-z0-9]+)*/)?"
                    r"[a-z0-9]+(?:-[a-z0-9]+)*[.]md", path,
                )
                or path.rsplit("/", 1)[-1] in {"index.md", "log.md"}):
            raise ValueError("Invalid or inconsistent theme member path.")
        return slug, path

    for item_index, raw_item in enumerate(_arr(source.get("items"), "batch.items"), 1):
        item = _obj(raw_item, "batch.items[]")
        if item.get("status") != "ok" or item.get("warnings"):
            incomplete = True
        for group_index, raw_group in enumerate(_arr(item.get("groups"), "item.groups"), 1):
            group = _obj(raw_group, "group")
            ref = f"i{item_index:04d}-g{group_index:04d}"
            members: set[tuple[str, str]] = set()
            for entry in _arr(group.get("members"), "group.members"):
                pair = member_ref(entry)
                if pair in members:
                    raise ValueError(f"Duplicate member in registry group {ref}.")
                members.add(pair)
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
        theme_list = _arr(obj.get("members"), "theme.members")
        theme_members = {member_ref(member) for member in theme_list}
        if len(theme_members) != len(theme_list):
            raise ValueError("Duplicate theme concept membership.")
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

def prepare_okf_scan_tasks(sections: Any) -> dict[str, Any]:
    """Convert exact Markdown section boundaries to one uniform scan task list."""
    record = _obj(sections, "sections")
    source = _str(record.get("source_path"), "sections.source_path")
    mode = record.get("mode")
    if mode == "direct":
        total_lines = record.get("total_lines")
        if type(total_lines) is not int or total_lines < 1:
            raise ValueError("Empty/invalid direct doctrine cannot be scanned.")
        return {"source_path": source, "tasks": [
            {"id": "direct", "start_line": 1, "end_line": total_lines},
        ]}
    if mode != "sections":
        raise ValueError("Unknown Markdown sections mode.")
    ranges = _arr(record.get("sections"), "sections.sections")
    if not 1 <= len(ranges) <= 512:
        raise ValueError("Section count exceeds 1..512.")
    tasks = []
    next_start = 1
    seen = set()
    for raw in ranges:
        entry = _obj(raw, "section")
        sid = _str(entry.get("id"), "section.id")
        start, end = entry.get("start_line"), entry.get("end_line")
        if (sid in seen or type(start) is not int or type(end) is not int
                or start != next_start or end < start):
            raise ValueError("Missing/overlapping section ranges.")
        tasks.append({"id": sid, "start_line": start, "end_line": end})
        seen.add(sid)
        next_start = end + 1
    if next_start - 1 != record.get("total_lines"):
        raise ValueError("Section boundaries do not cover whole document.")
    return {"source_path": source, "tasks": tasks}


def assemble_okf_section_outline(
    *, sections: Any, scans: Any, source_path: str, source_slug: str,
) -> dict[str, Any]:
    """Create a compact document topic outline from ALL checked section scans."""
    from cli_agent.workflows.flow_json_validation import _validate_doctrine
    source = _str(source_path, "source_path")
    slug = _str(source_slug, "source_slug")
    descriptor = _obj(sections, "sections")
    if descriptor.get("source_path") != source:
        raise ValueError("Outline sections belong to another document.")
    tasks = prepare_okf_scan_tasks(descriptor)["tasks"]
    expected = {t["id"]: t for t in tasks}
    seen: set[str] = set()
    concepts = []
    used: set[str] = set()
    for raw in _arr(scans, "scans"):
        scan = _obj(raw, "scan")
        sid = _str(scan.get("section_id"), "scan.section_id")
        if sid not in expected or sid in seen:
            raise ValueError("Missing, duplicate or unexpected section scan.")
        seen.add(sid)
        task = expected[sid]
        if (scan.get("status") != "ok" or scan.get("warnings")
                or scan.get("source_path") != source
                or scan.get("document_id") != slug
                or scan.get("chunk_id") != sid
                or scan.get("folder_id") != slug
                or scan.get("start_line") != task["start_line"]
                or scan.get("end_line") != task["end_line"]):
            raise ValueError(f"Section scan {sid} is incomplete/inconsistent.")
        checks = _obj(scan.get("scan_checks"), "scan_checks")
        if (checks.get("normative_sweep") != "completed"
                or checks.get("descriptive_sweep") != "completed"):
            raise ValueError(f"Section scan {sid} lacks a complete knowledge sweep.")
        local_keys: set[str] = set()
        for item in _arr(scan.get("candidate_concepts"), "candidate_concepts"):
            candidate = _obj(item, "candidate")
            key = _str(candidate.get("topic_key"), "topic_key")
            title = _str(candidate.get("name"), "candidate.name")
            desc = _str(candidate.get("description"), "candidate.description")
            if key in used or key in local_keys:
                raise ValueError("Duplicate source topic_key.")
            if not re.fullmatch(r"[a-z][a-z0-9-]*", key):
                raise ValueError("Unsafe source topic_key.")
            local_keys.add(key)
            used.add(key)
            concepts.append({
                "id": key, "path": f"doctrines/{slug}/{key}.md",
                "title": title, "description": desc,
                "type": "concept", "tags": [],
                "section_ids": [] if sid == "direct" else [sid],
            })
        items = _arr(scan.get("knowledge_items"), "knowledge_items")
        referenced: set[str] = set()
        for item in items:
            key = _str(_obj(item, "knowledge item").get("topic_key"), "topic_key")
            if key not in local_keys:
                raise ValueError("Knowledge item is not assigned to an outline topic.")
            referenced.add(key)
        if local_keys - referenced:
            raise ValueError("Candidate topic has no supported knowledge item.")
        if items and not local_keys:
            raise ValueError("Missing topics for source knowledge items.")
    if seen != set(expected) or not concepts:
        raise ValueError("Incomplete scans or no meaningful document topics.")
    result = {
        "status": "ok", "source_path": source,
        "folder": {"id": slug, "path": f"doctrines/{slug}",
                   "title": source.rsplit("/", 1)[-1],
                   "description": "Knowledge derived from a source document"},
        "folders": [], "summary": "Complete per-section topic inventory",
        "concepts": concepts, "warnings": [],
    }
    _validate_doctrine(result)
    return result
