"""Deterministic safety checks for LLM-proposed OKF output paths.

The model remains responsible for content and concept structure. This module
only rejects unsafe or ambiguous write targets before write-capable steps.
"""
from __future__ import annotations

import re
from typing import Any

_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"{prefix}{i}" for prefix in ("com", "lpt") for i in range(1, 10)),
}


def _obj(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a JSON object.")
    return value


def _array(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a JSON list.")
    return value


def _slug(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _SLUG.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase kebab-case slug.")
    if value.casefold() in _WINDOWS_RESERVED:
        raise ValueError(f"{name} is a reserved Windows filename.")
    return value


def _unique(seen: set[str], value: str, name: str) -> None:
    key = value.casefold()
    if key in seen:
        raise ValueError(f"{name} is duplicated: {value!r}.")
    seen.add(key)


def _canonical_source_path(value: Any) -> str:
    """Require a canonical workspace-relative Markdown path, on all OSes."""
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 4096
        or value != value.strip()
        or chr(92) in value
        or ":" in value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError("source_path is not a valid relative source path.")
    segments = value.split("/")
    if any(
        segment in {"", ".", ".."}
        or segment != segment.strip()
        or segment.endswith(".")
        or segment.split(".", 1)[0].casefold() in _WINDOWS_RESERVED
        for segment in segments
    ):
        raise ValueError("source_path must be a canonical workspace path.")
    if not segments[-1].casefold().endswith((".md", ".markdown")):
        raise ValueError("source_path must reference a Markdown file.")
    return value


def _validate_doctrine(plan: dict[str, Any]) -> None:
    folder = _obj(plan.get("folder"), "folder")
    folder_id = _slug(folder.get("id"), "folder.id")
    base = f"doctrines/{folder_id}"
    if folder.get("path") != base:
        raise ValueError("folder.path does not match folder.id.")

    subfolders = _array(plan.get("folders"), "folders")
    if len(subfolders) > 12:
        raise ValueError("Zu viele semantische Unterordner (maximal 12).")
    child_paths: set[str] = set()
    folder_ids: set[str] = set()
    for raw in subfolders:
        child = _obj(raw, "folders[]")
        child_id = _slug(child.get("id"), "folders[].id")
        _unique(folder_ids, child_id, "folders[].id")
        child_path = f"{base}/{child_id}"
        if child.get("path") != child_path:
            raise ValueError("Semantic subfolder escapes the doctrine folder.")
        child_paths.add(child_path)

    concepts = _array(plan.get("concepts"), "concepts")
    if not concepts:
        raise ValueError("An empty concept plan cannot be published.")
    if len(concepts) > 1000:
        raise ValueError("Concept plan exceeds 1000 concepts.")
    concept_ids: set[str] = set()
    concept_paths: set[str] = set()
    for raw in concepts:
        concept = _obj(raw, "concepts[]")
        concept_id = _slug(concept.get("id"), "concepts[].id")
        _unique(concept_ids, concept_id, "concepts[].id")
        path = concept.get("path")
        if not isinstance(path, str) or "\\" in path:
            raise ValueError("Invalid concept path.")
        parts = path.split("/")
        if len(parts) not in (3, 4) or not path.endswith(".md"):
            raise ValueError("Concept path has an invalid depth or extension.")
        filename = parts[-1][:-3]
        _slug(filename, "concept filename")
        if filename in {"index", "log"}:
            raise ValueError("index.md and log.md are not concept files.")
        parent = "/".join(parts[:-1])
        if (parent != base if not subfolders else parent not in child_paths):
            raise ValueError("Concept is outside the planned doctrine folder.")
        _unique(concept_paths, path, "concepts[].path")


def _validate_themes(plan: dict[str, Any]) -> None:
    themes = _array(plan.get("themes"), "themes")
    if len(themes) > 20:
        raise ValueError("Too many global themes (maximum 20).")
    ids: set[str] = set()
    for raw in themes:
        theme = _obj(raw, "themes[]")
        theme_id = _slug(theme.get("id"), "themes[].id")
        _unique(ids, theme_id, "themes[].id")
        # Equality, not path normalization: forbids ../, absolute paths,
        # Windows separators, nested directories and special names.
        expected = f"themen/{theme_id}.md"
        if theme.get("path") != expected or theme_id in {"index", "log"}:
            raise ValueError(
                f"Unsafe theme path for {theme_id!r}: required {expected!r}."
            )
        members = _array(theme.get("concept_paths"), "themes[].concept_paths")
        if len(members) < 2:
            raise ValueError("Globales Thema braucht mindestens zwei Concepts.")
        linked: set[str] = set()
        doctrine_ids: set[str] = set()
        for member in members:
            if not isinstance(member, str) or "\\" in member:
                raise ValueError("Unsafe theme-to-concept link.")
            parts = member.split("/")
            if len(parts) not in (3, 4) or parts[0] != "doctrines":
                raise ValueError("Theme link escapes the doctrine concepts.")
            _slug(parts[1], "Theme doctrine identifier")
            for segment in parts[2:-1]:
                _slug(segment, "Theme concept folder")
            if not parts[-1].endswith(".md"):
                raise ValueError("Theme link is missing the .md extension.")
            _slug(parts[-1][:-3], "Theme concept name")
            if parts[-1] in {"index.md", "log.md"}:
                raise ValueError("A theme must not link navigation files as concepts.")
            _unique(linked, member, "themes[].concept_paths")
            doctrine_ids.add(parts[1])
        if len(doctrine_ids) < 2:
            raise ValueError("Theme does not link at least two doctrines.")


def validate_okf_plan(
    value: Any, *, plan_kind: str, expected_source_path: str | None = None,
    expected_source_slug: str | None = None, manifests: Any = None
) -> dict[str, Any]:
    """Return the original plan after bounded, structural path validation."""
    plan = _obj(value, "OKF-Plan")
    if plan.get("status") != "ok":
        raise ValueError("OKF plan was not successful; writing is not permitted.")
    if plan_kind == "doctrine":
        _validate_doctrine(plan)
        source_path = _canonical_source_path(plan.get("source_path"))
        if expected_source_slug is not None and plan["folder"]["id"] != _slug(expected_source_slug, "expected_source_slug"):
            raise ValueError("folder.id does not match the inventory.")
        if expected_source_path is not None and source_path != _canonical_source_path(
            expected_source_path
        ):
            raise ValueError("source_path does not match the inventoried source.")
    elif plan_kind == "themes":
        _validate_themes(plan)
        if manifests is not None:
            current_paths: set[str] = set()
            modules: set[str] = set()
            for raw in _array(manifests, "manifests"):
                module = _obj(raw, "manifests[]")
                if module.get("status") != "ok":
                    raise ValueError("Incomplete module manifest.")
                module_path = module.get("module_path")
                if not isinstance(module_path, str) or not module_path.startswith("doctrines/"):
                    raise ValueError("Invalid module path.")
                _unique(modules, module_path, "module_path")
                for raw_concept in _array(module.get("concepts"), "manifest.concepts"):
                    concept_path = _obj(raw_concept, "manifest.concept").get("path")
                    if not isinstance(concept_path, str) or not concept_path.startswith(module_path + "/"):
                        raise ValueError("Invalid concept path in manifest.")
                    _unique(current_paths, concept_path, "manifest.concept.path")
            for theme in plan["themes"]:
                missing = {path for path in theme["concept_paths"] if path.casefold() not in current_paths}
                if missing:
                    raise ValueError("Thema referenziert veraltete Concepts: " + ", ".join(sorted(missing)))
    else:
        raise ValueError(f"Unknown OKF plan type: {plan_kind!r}.")
    return plan


def build_okf_manifest(
    *, plan: Any, navigation: Any, expected_source_path: str
) -> dict[str, Any]:
    """Build a lossless compact concept catalog; never ask an LLM to recopy it.

    The validated plan is the single source of concept identity and metadata.
    Navigation determines publication status, not membership. Any incomplete
    navigation yields needs_attention without advertising a complete module.
    """
    validated = validate_okf_plan(
        plan, plan_kind="doctrine", expected_source_path=expected_source_path
    )
    outcome = _obj(navigation, "navigation")
    folder = _obj(validated["folder"], "folder")
    folder_path = folder["path"]
    if outcome.get("status") not in {"ok", "needs_attention"}:
        raise ValueError("navigation.status must be ok or needs_attention.")
    if outcome.get("module_path") != folder_path:
        raise ValueError("navigation.module_path does not match the plan.")

    warnings = outcome.get("warnings", [])
    if not isinstance(warnings, list) or not all(
        isinstance(warning, str) for warning in warnings
    ):
        raise ValueError("navigation.warnings must be a list of strings.")
    complete = outcome["status"] == "ok"
    if complete:
        indexes = _array(outcome.get("index_paths"), "navigation.index_paths")
        required = {f"{folder_path}/index.md"} | {
            f"{entry['path']}/index.md" for entry in validated["folders"]
        }
        if len(indexes) != len(required) or set(indexes) != required:
            raise ValueError(
                "navigation.index_paths does not match the planned indexes exactly."
            )
    source_path = _canonical_source_path(validated.get("source_path"))

    concepts: list[dict[str, Any]] = []
    if complete:
        for raw in validated["concepts"]:
            concept = _obj(raw, "concept")
            title = concept.get("title")
            description = concept.get("description")
            tags = concept.get("tags", [])
            if not isinstance(title, str) or not title.strip():
                raise ValueError(f"Concept {concept['id']}: title is missing.")
            if not isinstance(description, str) or not description.strip():
                raise ValueError(f"Concept {concept['id']}: description is missing.")
            if not isinstance(tags, list) or not all(
                isinstance(tag, str) and tag.strip() for tag in tags
            ):
                raise ValueError(f"Concept {concept['id']}: invalid tags.")
            concepts.append({
                "path": concept["path"],
                "title": title,
                "description": description,
                "tags": list(tags),
            })
    title = folder.get("title")
    description = folder.get("description")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("folder.title is missing from the doctrine plan.")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("folder.description is missing from the doctrine plan.")

    return {
        "status": "ok" if complete else "needs_attention",
        "source_path": source_path,
        "module_path": folder_path,
        "title": title,
        "description": description,
        "concepts": concepts,
        "warnings": warnings,
    }


def prepare_doctrine_concept_batches(
    structure: Any, analyses: Any, *, batch_size: int = 3,
) -> dict[str, Any]:
    """Prepare bounded detail tasks without altering the model's global structure."""
    from cli_agent.workflows.flow_json_operations import _serialized_size
    plan = _obj(structure, "structure")
    if plan.get("status") != "ok":
        raise ValueError("Global concept structure was not successful.")
    if type(batch_size) is not int or not 1 <= batch_size <= 10:
        raise ValueError("batch_size must be between 1 and 10.")
    # Run path/identity checks before allowing a writer to consume the structure.
    _validate_doctrine(plan)
    source = _canonical_source_path(plan.get("source_path"))
    sections = [
        _obj(x, "section_analyses.iterations[]").get("output")
        for x in _array(_obj(analyses, "section_analyses").get("iterations"), "section_analyses.iterations")
    ]
    if len(sections) > 100:
        raise ValueError("Zu viele Abschnittsanalysen.")
    verified: list[dict[str, Any]] = []
    for section in sections:
        record = _obj(section, "section_analyses[]")
        if record.get("status") != "ok" or record.get("source_path") != source:
            raise ValueError("Incomplete or inconsistent section analysis.")
        if (type(record.get("start_line")) is not int
                or type(record.get("end_line")) is not int
                or record["start_line"] < 1
                or record["end_line"] < record["start_line"]):
            raise ValueError("Invalid section boundaries.")
        verified.append(record)
    verified.sort(key=lambda x: (x["start_line"], x["end_line"]))
    if verified and any(
        a["end_line"] + 1 != b["start_line"]
        for a, b in zip(verified, verified[1:])
    ):
        raise ValueError("Section analyses have gaps or overlaps.")
    section_by_id = {}
    for section in verified:
        sid = section.get("section_id")
        if not isinstance(sid, str) or sid in section_by_id:
            raise ValueError("Section analyses have invalid IDs.")
        section_by_id[sid] = section
    batches = []
    concepts = plan["concepts"]
    for offset in range(0, len(concepts), batch_size):
        chunk = concepts[offset:offset + batch_size]
        batch_id = f"b{len(batches)+1:04d}"
        selected = set()
        for concept in chunk:
            ids = _array(concept.get("section_ids"), "concept.section_ids")
            if (bool(section_by_id) != bool(ids)
                    or any(not isinstance(sid, str) or sid not in section_by_id for sid in ids)):
                raise ValueError("Concept contains invalid section references.")
            selected.update(ids)
        batch = {
            "id": batch_id,
            "source_path": source,
            "concepts": chunk,
            "sections": [section_by_id[sid] for sid in section_by_id if sid in selected],
        }
        if _serialized_size(batch) > 1_000_000:
            raise ValueError("Concept group exceeds 1 MB of input data.")
        batches.append(batch)
    if len(batches) > 1000:
        raise ValueError("Zu viele Konzeptgruppen.")
    return {"batches": batches, "total_concepts": len(concepts)}


def assemble_doctrine_concept_plan(
    structure: Any, results: Any, batches: Any,
) -> dict[str, Any]:
    """Merge exact, complete evidence for each globally planned concept."""
    plan = _obj(structure, "structure")
    if plan.get("status") != "ok":
        raise ValueError("Global concept structure was not successful.")
    _validate_doctrine(plan)
    records = _array(
        _obj(results, "detail_results").get("iterations"),
        "detail_results.iterations",
    )
    expected = {concept["id"]: concept for concept in plan["concepts"]}
    received: dict[str, dict[str, Any]] = {}
    source = _canonical_source_path(plan.get("source_path"))
    expected_groups: dict[str, dict[str, Any]] = {}
    for batch in _array(_obj(batches, "batches").get("batches"), "batches.batches"):
        group = _obj(batch, "batches.batches[]")
        gid = group.get("id")
        if not isinstance(gid, str) or gid in expected_groups:
            raise ValueError("Invalid or duplicate batch ID.")
        if group.get("source_path") != source:
            raise ValueError("Batch references a different doctrine.")
        expected_groups[gid] = group
    seen_groups: set[str] = set()
    for entry in records:
        envelope = _obj(entry, "detail_results.iterations[]")
        group_id = envelope.get("id")
        if group_id not in expected_groups or group_id in seen_groups:
            raise ValueError("Detail plan contains unknown or duplicate batch IDs.")
        seen_groups.add(group_id)
        group = expected_groups[group_id]
        item = _obj(envelope.get("output"), "detail_results.iterations[].output")
        if item.get("status") != "ok":
            raise ValueError("Incomplete detail plan.")
        details = _array(item.get("concepts"), "detail_results[].concepts")
        allowed_group_ids = {
            _obj(concept, "batches.concepts[]").get("id")
            for concept in _array(group.get("concepts"), "batches.concepts")
        }
        if len(details) != len(allowed_group_ids):
            raise ValueError("Batch does not contain all planned concepts.")
        for detail in details:
            concept = _obj(detail, "detail_results[].concepts[]")
            identity = concept.get("id")
            if identity not in expected or identity not in allowed_group_ids or identity in received:
                raise ValueError("Unknown or multiply planned concept.")
            original = expected[identity]
            for field in ("path", "title", "description", "type", "tags"):
                if field in concept and concept[field] != original.get(field):
                    raise ValueError("Detail plan must not alter concept structure.")
            ranges = _array(concept.get("evidence_ranges"), "evidence_ranges")
            if not ranges:
                raise ValueError("Concept has no source evidence.")
            for evidence in ranges:
                ref = _obj(evidence, "evidence_ranges[]")
                if ref.get("doctrine") != source:
                    raise ValueError("Evidence references the wrong doctrine.")
                begin, end = ref.get("start_line"), ref.get("end_line")
                if (type(begin) is not int or type(end) is not int
                        or begin < 1 or end < begin):
                    raise ValueError("Invalid source range.")
                if group.get("sections"):
                    matching = [
                        section for section in group["sections"]
                        if section["start_line"] <= begin <= end <= section["end_line"]
                    ]
                    if not matching:
                        raise ValueError("Source range lies outside assigned sections.")
            received[identity] = concept
    if set(seen_groups) != set(expected_groups):
        raise ValueError("Detail plan does not include all batches.")
    if set(received) != set(expected):
        raise ValueError("Detail plan does not include all concepts.")
    merged = dict(plan)
    merged["concepts"] = [
        {
            **{key: value for key, value in concept.items() if key != "section_ids"},
            "evidence_ranges": received[concept["id"]]["evidence_ranges"],
        }
        for concept in plan["concepts"]
    ]
    _validate_doctrine(merged)
    return merged


def _section_concept_metadata(
    raw: dict[str, Any], *, section_id: str, index: int,
) -> tuple[str, str, list[str]]:
    """Validate local model metadata with useful errors and safe compatibility.

    Previously saved local-outline checkpoints can have a scalar or omitted
    tags field, or legitimately longer descriptive text. Preserve their
    semantic content without silently inventing missing descriptions.
    """
    origin = f"Section {section_id!r}, concept {index}"
    title = raw.get("title")
    if title is None:
        title = raw.get("name")
    if not isinstance(title, str) or not title.strip():
        raise ValueError(f"{origin}: title must be a nonempty string.")
    title = title.strip()
    if len(title) > 500:
        raise ValueError(
            f"{origin}: title contains {len(title)} characters (maximum 500)."
        )

    description = raw.get("description")
    if description is None:
        description = raw.get("summary")
    if not isinstance(description, str) or not description.strip():
        raise ValueError(
            f"{origin}: description must be a nonempty string."
        )
    description = description.strip()
    if len(description) > 4000:
        raise ValueError(
            f"{origin}: description contains {len(description)} characters "
            "(maximal 4000)."
        )

    tags = raw.get("tags")
    if tags is None:
        tags = []
    elif isinstance(tags, str):
        tags = [tags]
    elif not isinstance(tags, list):
        raise ValueError(
            f"{origin}: tags must be a list of strings."
        )
    if len(tags) > 40:
        raise ValueError(
            f"{origin}: tags contains {len(tags)} entries (maximum 40)."
        )
    normalized: list[str] = []
    seen: set[str] = set()
    for tag_index, value in enumerate(tags, start=1):
        if not isinstance(value, str):
            raise ValueError(
                f"{origin}: tags[{tag_index}] must be a string."
            )
        tag = value.strip()
        if not tag:
            continue
        if len(tag) > 160:
            raise ValueError(
                f"{origin}: tags[{tag_index}] contains {len(tag)} characters "
                "(maximal 160)."
            )
        if tag.casefold() not in seen:
            seen.add(tag.casefold())
            normalized.append(tag)
    return title, description, normalized


def assemble_section_outlines(
    tasks: Any, results: Any, *, source_path: str, source_slug: str,
) -> dict[str, Any]:
    """Assemble local outlines without another model request.

    Identical semantic keys share one concept across source sections; distinct
    keys retain their own concept. No source evidence is fabricated.
    """
    source_path = _canonical_source_path(source_path)
    source_slug = _slug(source_slug, "source_slug")
    task_list = _array(_obj(tasks, "outline_tasks").get("tasks"), "outline_tasks.tasks")
    iteration_list = _array(_obj(results, "outlines").get("iterations"), "outlines.iterations")
    if not 1 <= len(task_list) <= 100 or len(iteration_list) != len(task_list):
        raise ValueError("Section outlines are missing or too numerous.")
    expected = {}
    for task in task_list:
        task = _obj(task, "tasks[]")
        tid = task.get("id")
        if not isinstance(tid, str) or tid in expected:
            raise ValueError("Duplicate or invalid section ID.")
        expected[tid] = task
    merged: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    warnings: list[str] = []
    for iteration in iteration_list:
        envelope = _obj(iteration, "outlines.iterations[]")
        tid = envelope.get("id")
        if tid not in expected or tid in seen:
            raise ValueError("Unknown or duplicate section iteration.")
        seen.add(tid)
        output = _obj(envelope.get("output"), "outlines.iterations[].output")
        if output.get("status") != "ok" or output.get("section_id") != tid:
            raise ValueError("Section outline is incomplete or inconsistent.")
        local = _array(output.get("concepts"), "outlines.concepts")
        if len(local) > 100:
            raise ValueError("Section outline contains too many concepts.")
        for concept_index, entry in enumerate(local, start=1):
            concept = _obj(entry, f"outlines[{tid}].concepts[{concept_index}]")
            key = _slug(
                concept.get("topic_key"),
                f"Section {tid!r}, concept {concept_index}: topic_key",
            )
            title, description, tags = _section_concept_metadata(
                concept, section_id=tid, index=concept_index,
            )
            if key not in merged:
                merged[key] = {
                    "id": key, "path": f"doctrines/{source_slug}/{key}.md",
                    "title": title, "description": description,
                    "type": "concept", "tags": tags,
                    "section_ids": [] if expected[tid]["direct"] else [tid],
                }
            else:
                original = merged[key]
                if not expected[tid]["direct"] and tid not in original["section_ids"]:
                    original["section_ids"].append(tid)
                original["tags"] = list(dict.fromkeys([*original["tags"], *tags]))[:20]
        warnings.extend(str(w)[:300] for w in _array(output.get("warnings", []), "warnings")[:20])
    if seen != set(expected) or not merged:
        raise ValueError("Section outlines are missing or contain no concepts.")
    result = {
        "status": "ok", "source_path": source_path,
        "folder": {
            "id": source_slug, "path": f"doctrines/{source_slug}",
            "title": source_slug, "description": "Doctrine and its concepts",
        },
        "folders": [],
        "summary": "Merge of complete section outlines",
        "concepts": list(merged.values()), "warnings": warnings[:100],
    }
    _validate_doctrine(result)
    return result


def prepare_section_outline_tasks(sections: Any, analyses: Any) -> dict[str, Any]:
    """Attach only the matching analysis to each bounded outline request."""
    record = _obj(sections, "sections")
    source_path = _canonical_source_path(record.get("source_path"))
    items = _array(record.get("sections"), "sections.sections")
    iterations = _array(_obj(analyses, "analyses").get("iterations"), "analyses.iterations")
    direct = record.get("mode") == "direct"
    if record.get("mode") not in {"direct", "sections"}:
        raise ValueError("Unbekannter Abschnittsmodus.")
    if (direct and (items or iterations)) or (
        not direct and (not items or len(items) != len(iterations))
    ):
        raise ValueError("Section analyses are missing or inconsistent.")
    if direct:
        return {"tasks": [{"id": "direct", "direct": True, "source_path": source_path}]}
    indexed = {}
    for envelope in iterations:
        envelope = _obj(envelope, "analyses.iterations[]")
        aid = envelope.get("id")
        if not isinstance(aid, str) or aid in indexed:
            raise ValueError("Invalid or duplicate analysis ID.")
        indexed[aid] = _obj(envelope.get("output"), "analyses.output")
    tasks = []
    for item in items:
        section = _obj(item, "sections.sections[]")
        sid = section.get("id")
        analysis = indexed.get(sid)
        if (analysis is None or analysis.get("status") != "ok"
                or analysis.get("section_id") != sid
                or analysis.get("source_path") != source_path
                or analysis.get("start_line") != section.get("start_line")
                or analysis.get("end_line") != section.get("end_line")):
            raise ValueError("Incomplete or contradictory section analysis.")
        tasks.append({"id": sid, "direct": False, "source_path": source_path,
                      "analysis": analysis})
    if set(indexed) != {x["id"] for x in tasks}:
        raise ValueError("Unerwartete Abschnittsanalyse.")
    return {"tasks": tasks}


def build_doctrine_taxonomy_profile(outline: Any) -> dict[str, Any]:
    """Small topic inventory; never feed all candidates to a global model."""
    plan = _obj(outline, "outline")
    _validate_doctrine(plan)
    concepts = _array(plan["concepts"], "concepts")
    counts: dict[str, int] = {}
    for concept in concepts:
        for tag in concept.get("tags", [])[:12]:
            if isinstance(tag, str) and tag.strip():
                tag = tag.strip().casefold()[:80]
                counts[tag] = counts.get(tag, 0) + 1
    sample = [
        {"title": concepts[i]["title"][:110], "tags": concepts[i].get("tags", [])[:4]}
        for i in sorted({min(len(concepts) - 1, j * len(concepts) // 16) for j in range(16)})
    ]
    return {
        "candidate_count": len(concepts),
        "tags": [
            {"name": name, "count": count}
            for name, count in sorted(counts.items(), key=lambda p: (-p[1], p[0]))[:40]
        ],
        "sample_titles": sample,
    }


def _doctrine_folder_scheme(
    taxonomy: Any, *, concept_count: int,
) -> list[dict[str, str]]:
    raw = _obj(taxonomy, "taxonomy")
    if raw.get("status") != "ok":
        raise ValueError("Folder taxonomy is incomplete.")
    folders = _array(raw.get("folders"), "taxonomy.folders")
    if concept_count > 15 and not 2 <= len(folders) <= 8:
        raise ValueError("Large doctrines require 2 to 8 subfolders.")
    if concept_count <= 15 and len(folders) not in (0, 2, 3, 4, 5, 6, 7, 8):
        raise ValueError("Subfolders: either 0 or between 2 and 8.")
    seen: set[str] = set()
    normalized = []
    for entry in folders:
        folder = _obj(entry, "taxonomy.folders[]")
        identifier = _slug(folder.get("id"), "taxonomy.folders[].id")
        _unique(seen, identifier, "taxonomy.folders[].id")
        title = folder.get("title")
        description = folder.get("description")
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 120:
            raise ValueError("taxonomy.folders[].title is invalid.")
        if not isinstance(description, str) or not 1 <= len(description.strip()) <= 500:
            raise ValueError("taxonomy.folders[].description is invalid.")
        normalized.append({
            "id": identifier, "title": title.strip(),
            "description": description.strip(),
        })
    return normalized


def prepare_doctrine_consolidation_batches(
    outline: Any, taxonomy: Any, *, batch_size: int = 12,
) -> dict[str, Any]:
    """Cluster nearby topics into small, checkpointable semantic comparisons."""
    plan = _obj(outline, "outline")
    _validate_doctrine(plan)
    if type(batch_size) is not int or not 4 <= batch_size <= 16:
        raise ValueError("Consolidation batch_size must be between 4 and 16.")
    concepts = _array(plan["concepts"], "concepts")
    folders = _doctrine_folder_scheme(taxonomy, concept_count=len(concepts))
    stop = {"und", "der", "die", "das", "ein", "eine", "von", "für", "mit",
            "bei", "des", "den", "and", "the", "for", "from", "with"}
    def words(concept: dict[str, Any]) -> set[str]:
        text = " ".join([
            concept["id"].replace("-", " "), concept.get("title", ""),
            concept.get("description", "")[:400],
            " ".join(t for t in concept.get("tags", []) if isinstance(t, str)),
        ]).casefold()
        return {w for w in re.findall(r"[\w]{3,}", text) if w not in stop}
    tokens = [words(concept) for concept in concepts]
    remaining = list(range(len(concepts)))
    batches = []
    while remaining:
        first = remaining.pop(0)
        selected = [first]
        while remaining and len(selected) < batch_size:
            # Similar topics in one model call; comparisons scale with candidates,
            # not model context length. Stable tie-breaker preserves checkpoints.
            best = max(
                remaining,
                key=lambda i: (
                    max(
                        len(tokens[i] & tokens[j]) /
                        max(1, len(tokens[i] | tokens[j]))
                        for j in selected
                    ),
                    -i,
                ),
            )
            remaining.remove(best)
            selected.append(best)
        candidates = []
        for i in selected:
            c = concepts[i]
            candidates.append({
                "id": c["id"], "title": c["title"][:250],
                "description": c["description"][:500],
                "tags": c.get("tags", [])[:10],
            })
        batches.append({
            "id": f"c{len(batches) + 1:04d}",
            "candidates": candidates,
            "folders": folders,
        })
        if len(batches) > 250:
            raise ValueError("Zu viele Konsolidierungsgruppen.")
    return {"batches": batches, "candidate_count": len(concepts)}


def assemble_doctrine_consolidation(
    outline: Any, taxonomy: Any, batches: Any, results: Any,
) -> dict[str, Any]:
    """Reject any missing/duplicate source; preserve all sections and tags."""
    plan = _obj(outline, "outline")
    _validate_doctrine(plan)
    source = _canonical_source_path(plan.get("source_path"))
    folder_id = _slug(_obj(plan.get("folder"), "folder").get("id"), "folder.id")
    original = {c["id"]: c for c in _array(plan["concepts"], "concepts")}
    folders = _doctrine_folder_scheme(taxonomy, concept_count=len(original))
    allowed_folders = {folder["id"] for folder in folders}
    expected = {}
    for raw in _array(_obj(batches, "batches").get("batches"), "batches.batches"):
        group = _obj(raw, "batches[]")
        gid = group.get("id")
        if not isinstance(gid, str) or gid in expected:
            raise ValueError("Invalid or duplicate consolidation batch ID.")
        ids = [_obj(c, "candidates[]").get("id")
               for c in _array(group.get("candidates"), "batches.candidates")]
        if not ids or len(ids) > 16 or len(ids) != len(set(ids)):
            raise ValueError("Invalid concepts in consolidation batch.")
        expected[gid] = set(ids)
    if set().union(*expected.values()) != set(original) or sum(
        len(group) for group in expected.values()
    ) != len(original):
        raise ValueError("Consolidation batches contain gaps or duplicates.")
    envelopes = _array(_obj(results, "results").get("iterations"), "results.iterations")
    seen_batches: set[str] = set()
    seen_concepts: set[str] = set()
    consolidated = []
    for envelope in envelopes:
        record = _obj(envelope, "results.iterations[]")
        bid = record.get("id")
        if bid not in expected or bid in seen_batches:
            raise ValueError("Unknown or duplicate consolidation response.")
        seen_batches.add(bid)
        response = _obj(record.get("output"), "results.output")
        if response.get("status") != "ok":
            raise ValueError(f"Consolidation for {bid} was unsuccessful.")
        local_groups = _array(response.get("groups"), "results.groups")
        if not local_groups:
            raise ValueError(f"Consolidation for {bid} is empty.")
        used: set[str] = set()
        for group in local_groups:
            value = _obj(group, "results.groups[]")
            members = _array(value.get("member_ids"), "groups.member_ids")
            if not members or any(
                not isinstance(m, str) or m not in expected[bid] or m in used
                for m in members
            ):
                raise ValueError(f"{bid}: unknown or duplicate source concept.")
            used.update(members)
            title = value.get("title")
            desc = value.get("description")
            if not isinstance(title, str) or not 1 <= len(title.strip()) <= 500:
                raise ValueError(f"{bid}: invalid concept title.")
            if not isinstance(desc, str) or not 1 <= len(desc.strip()) <= 4000:
                raise ValueError(f"{bid}: invalid concept description.")
            target = value.get("folder_id")
            if (target not in allowed_folders if folders else target not in (None, "")):
                raise ValueError(f"{bid}: unbekannter semantischer Unterordner.")
            first = original[members[0]]
            section_ids = list(dict.fromkeys(
                sid for member in members
                for sid in original[member].get("section_ids", [])
            ))
            source_ids = list(dict.fromkeys(
                ref for member in members
                for ref in original[member].get("merged_from", [member])
            ))
            tags = list(dict.fromkeys(
                tag for member in members
                for tag in original[member].get("tags", [])
            ))[:40]
            canonical_id = first["id"]
            parent = f"doctrines/{folder_id}"
            if folders:
                parent += f"/{target}"
            consolidated.append({
                "id": canonical_id,
                "path": f"{parent}/{canonical_id}.md",
                "title": title.strip(), "description": desc.strip(),
                "type": "concept", "tags": tags,
                "section_ids": section_ids, "merged_from": source_ids,
            })
        if used != expected[bid]:
            raise ValueError(f"{bid}: not all source concepts were included.")
        seen_concepts.update(used)
    if seen_batches != set(expected) or seen_concepts != set(original):
        raise ValueError("Consolidation is incomplete.")
    output = dict(plan)
    output["source_path"] = source
    output["folders"] = [
        {
            **folder,
            "path": f"doctrines/{folder_id}/{folder['id']}",
        }
        for folder in folders
    ]
    output["concepts"] = consolidated
    output["summary"] = (
        f"{len(original)} concept candidates fully consolidated into "
        f"{len(consolidated)} concepts consolidated."
    )
    _validate_doctrine(output)
    return output
