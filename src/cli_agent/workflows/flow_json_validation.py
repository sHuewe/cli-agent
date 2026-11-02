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
        raise ValueError(f"{name} muss ein JSON-Objekt sein.")
    return value


def _array(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{name} muss eine JSON-Liste sein.")
    return value


def _slug(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _SLUG.fullmatch(value):
        raise ValueError(f"{name} muss ein lowercase kebab-case Slug sein.")
    if value.casefold() in _WINDOWS_RESERVED:
        raise ValueError(f"{name} ist ein reservierter Windows-Dateiname.")
    return value


def _unique(seen: set[str], value: str, name: str) -> None:
    key = value.casefold()
    if key in seen:
        raise ValueError(f"{name} ist doppelt: {value!r}.")
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
        raise ValueError("source_path ist kein gültiger relativer Quellpfad.")
    segments = value.split("/")
    if any(
        segment in {"", ".", ".."}
        or segment != segment.strip()
        or segment.endswith(".")
        or segment.split(".", 1)[0].casefold() in _WINDOWS_RESERVED
        for segment in segments
    ):
        raise ValueError("source_path muss ein kanonischer Workspace-Pfad sein.")
    if not segments[-1].casefold().endswith((".md", ".markdown")):
        raise ValueError("source_path muss auf eine Markdown-Datei verweisen.")
    return value


def _validate_doctrine(plan: dict[str, Any]) -> None:
    folder = _obj(plan.get("folder"), "folder")
    folder_id = _slug(folder.get("id"), "folder.id")
    base = f"doctrines/{folder_id}"
    if folder.get("path") != base:
        raise ValueError("folder.path stimmt nicht mit folder.id überein.")

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
            raise ValueError("Semantischer Unterordner verlässt den Doctrine-Ordner.")
        child_paths.add(child_path)

    concepts = _array(plan.get("concepts"), "concepts")
    if not concepts:
        raise ValueError("Leerer Concept-Plan darf nicht veröffentlicht werden.")
    if len(concepts) > 1000:
        raise ValueError("Concept-Plan überschreitet 1000 Konzepte.")
    concept_ids: set[str] = set()
    concept_paths: set[str] = set()
    for raw in concepts:
        concept = _obj(raw, "concepts[]")
        concept_id = _slug(concept.get("id"), "concepts[].id")
        _unique(concept_ids, concept_id, "concepts[].id")
        path = concept.get("path")
        if not isinstance(path, str) or "\\" in path:
            raise ValueError("Ungültiger Concept-Pfad.")
        parts = path.split("/")
        if len(parts) not in (3, 4) or not path.endswith(".md"):
            raise ValueError("Concept-Pfad hat eine ungültige Tiefe oder Endung.")
        filename = parts[-1][:-3]
        _slug(filename, "concept filename")
        if filename in {"index", "log"}:
            raise ValueError("index.md und log.md sind keine Concept-Dateien.")
        parent = "/".join(parts[:-1])
        if (parent != base if not subfolders else parent not in child_paths):
            raise ValueError("Concept liegt nicht im geplanten Doctrine-Ordner.")
        _unique(concept_paths, path, "concepts[].path")


def _validate_themes(plan: dict[str, Any]) -> None:
    themes = _array(plan.get("themes"), "themes")
    if len(themes) > 20:
        raise ValueError("Zu viele globale Themen (maximal 20).")
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
                f"Unsicherer Themenpfad für {theme_id!r}: erforderlich {expected!r}."
            )
        members = _array(theme.get("concept_paths"), "themes[].concept_paths")
        if len(members) < 2:
            raise ValueError("Globales Thema braucht mindestens zwei Concepts.")
        linked: set[str] = set()
        doctrine_ids: set[str] = set()
        for member in members:
            if not isinstance(member, str) or "\\" in member:
                raise ValueError("Unsicherer Theme-Concept-Link.")
            parts = member.split("/")
            if len(parts) not in (3, 4) or parts[0] != "doctrines":
                raise ValueError("Theme-Link verlässt die Doctrine-Concepts.")
            _slug(parts[1], "Theme doctrine identifier")
            for segment in parts[2:-1]:
                _slug(segment, "Theme concept folder")
            if not parts[-1].endswith(".md"):
                raise ValueError("Theme-Link ohne .md-Endung.")
            _slug(parts[-1][:-3], "Theme concept name")
            if parts[-1] in {"index.md", "log.md"}:
                raise ValueError("Theme darf keine Navigation als Concept verlinken.")
            _unique(linked, member, "themes[].concept_paths")
            doctrine_ids.add(parts[1])
        if len(doctrine_ids) < 2:
            raise ValueError("Thema verknüpft nicht mindestens zwei Doctrines.")


def validate_okf_plan(
    value: Any, *, plan_kind: str, expected_source_path: str | None = None,
    expected_source_slug: str | None = None, manifests: Any = None
) -> dict[str, Any]:
    """Return the original plan after bounded, structural path validation."""
    plan = _obj(value, "OKF-Plan")
    if plan.get("status") != "ok":
        raise ValueError("OKF-Plan ist nicht erfolgreich; kein Schreibschritt erlaubt.")
    if plan_kind == "doctrine":
        _validate_doctrine(plan)
        source_path = _canonical_source_path(plan.get("source_path"))
        if expected_source_slug is not None and plan["folder"]["id"] != _slug(expected_source_slug, "expected_source_slug"):
            raise ValueError("folder.id stimmt nicht mit der Inventur überein.")
        if expected_source_path is not None and source_path != _canonical_source_path(
            expected_source_path
        ):
            raise ValueError("source_path stimmt nicht mit der inventarisierten Quelle überein.")
    elif plan_kind == "themes":
        _validate_themes(plan)
        if manifests is not None:
            current_paths: set[str] = set()
            modules: set[str] = set()
            for raw in _array(manifests, "manifests"):
                module = _obj(raw, "manifests[]")
                if module.get("status") != "ok":
                    raise ValueError("Unvollständiges Modulmanifest.")
                module_path = module.get("module_path")
                if not isinstance(module_path, str) or not module_path.startswith("doctrines/"):
                    raise ValueError("Ungültiger Modulpfad.")
                _unique(modules, module_path, "module_path")
                for raw_concept in _array(module.get("concepts"), "manifest.concepts"):
                    concept_path = _obj(raw_concept, "manifest.concept").get("path")
                    if not isinstance(concept_path, str) or not concept_path.startswith(module_path + "/"):
                        raise ValueError("Ungültiger Concept-Pfad im Manifest.")
                    _unique(current_paths, concept_path, "manifest.concept.path")
            for theme in plan["themes"]:
                missing = {path for path in theme["concept_paths"] if path.casefold() not in current_paths}
                if missing:
                    raise ValueError("Thema referenziert veraltete Concepts: " + ", ".join(sorted(missing)))
    else:
        raise ValueError(f"Unbekannte OKF-Planart: {plan_kind!r}.")
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
        raise ValueError("navigation.status muss ok|needs_attention sein.")
    if outcome.get("module_path") != folder_path:
        raise ValueError("navigation.module_path stimmt nicht mit Plan überein.")

    warnings = outcome.get("warnings", [])
    if not isinstance(warnings, list) or not all(
        isinstance(warning, str) for warning in warnings
    ):
        raise ValueError("navigation.warnings muss eine String-Liste sein.")
    complete = outcome["status"] == "ok"
    if complete:
        indexes = _array(outcome.get("index_paths"), "navigation.index_paths")
        required = {f"{folder_path}/index.md"} | {
            f"{entry['path']}/index.md" for entry in validated["folders"]
        }
        if len(indexes) != len(required) or set(indexes) != required:
            raise ValueError(
                "navigation.index_paths deckt nicht genau die geplanten Indizes ab."
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
                raise ValueError(f"Concept {concept['id']}: title fehlt.")
            if not isinstance(description, str) or not description.strip():
                raise ValueError(f"Concept {concept['id']}: description fehlt.")
            if not isinstance(tags, list) or not all(
                isinstance(tag, str) and tag.strip() for tag in tags
            ):
                raise ValueError(f"Concept {concept['id']}: ungültige Tags.")
            concepts.append({
                "path": concept["path"],
                "title": title,
                "description": description,
                "tags": list(tags),
            })
    title = folder.get("title")
    description = folder.get("description")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("folder.title fehlt im Doctrine-Plan.")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("folder.description fehlt im Doctrine-Plan.")

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
        raise ValueError("Globale Konzeptstruktur ist nicht erfolgreich.")
    if type(batch_size) is not int or not 1 <= batch_size <= 10:
        raise ValueError("batch_size muss zwischen 1 und 10 liegen.")
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
            raise ValueError("Unvollständige oder inkonsistente Abschnittsanalyse.")
        if (type(record.get("start_line")) is not int
                or type(record.get("end_line")) is not int
                or record["start_line"] < 1
                or record["end_line"] < record["start_line"]):
            raise ValueError("Ungültige Abschnittsgrenzen.")
        verified.append(record)
    verified.sort(key=lambda x: (x["start_line"], x["end_line"]))
    if verified and any(
        a["end_line"] + 1 != b["start_line"]
        for a, b in zip(verified, verified[1:])
    ):
        raise ValueError("Abschnittsanalysen weisen Lücken oder Überlappungen auf.")
    section_by_id = {}
    for section in verified:
        sid = section.get("section_id")
        if not isinstance(sid, str) or sid in section_by_id:
            raise ValueError("Abschnittsanalysen haben ungültige IDs.")
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
                raise ValueError("Konzept enthält ungültige Abschnittsreferenzen.")
            selected.update(ids)
        batch = {
            "id": batch_id,
            "source_path": source,
            "concepts": chunk,
            "sections": [section_by_id[sid] for sid in section_by_id if sid in selected],
        }
        if _serialized_size(batch) > 1_000_000:
            raise ValueError("Konzeptgruppe überschreitet 1 MB Eingabedaten.")
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
        raise ValueError("Globale Konzeptstruktur ist nicht erfolgreich.")
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
            raise ValueError("Ungültige oder doppelte Batch-ID.")
        if group.get("source_path") != source:
            raise ValueError("Batch verweist auf eine andere Doktrin.")
        expected_groups[gid] = group
    seen_groups: set[str] = set()
    for entry in records:
        envelope = _obj(entry, "detail_results.iterations[]")
        group_id = envelope.get("id")
        if group_id not in expected_groups or group_id in seen_groups:
            raise ValueError("Detailplanung enthält unbekannte oder doppelte Batch-ID.")
        seen_groups.add(group_id)
        group = expected_groups[group_id]
        item = _obj(envelope.get("output"), "detail_results.iterations[].output")
        if item.get("status") != "ok":
            raise ValueError("Unvollständige Detailplanung.")
        details = _array(item.get("concepts"), "detail_results[].concepts")
        allowed_group_ids = {
            _obj(concept, "batches.concepts[]").get("id")
            for concept in _array(group.get("concepts"), "batches.concepts")
        }
        if len(details) != len(allowed_group_ids):
            raise ValueError("Batch enthält nicht alle vorgesehenen Konzepte.")
        for detail in details:
            concept = _obj(detail, "detail_results[].concepts[]")
            identity = concept.get("id")
            if identity not in expected or identity not in allowed_group_ids or identity in received:
                raise ValueError("Unbekanntes oder mehrfach geplantes Konzept.")
            original = expected[identity]
            for field in ("path", "title", "description", "type", "tags"):
                if field in concept and concept[field] != original.get(field):
                    raise ValueError("Detailplanung darf Konzeptstruktur nicht ändern.")
            ranges = _array(concept.get("evidence_ranges"), "evidence_ranges")
            if not ranges:
                raise ValueError("Konzept ohne Quellenbelege.")
            for evidence in ranges:
                ref = _obj(evidence, "evidence_ranges[]")
                if ref.get("doctrine") != source:
                    raise ValueError("Beleg verweist auf falsche Doktrin.")
                begin, end = ref.get("start_line"), ref.get("end_line")
                if (type(begin) is not int or type(end) is not int
                        or begin < 1 or end < begin):
                    raise ValueError("Ungültiger Quellenbereich.")
                if group.get("sections"):
                    matching = [
                        section for section in group["sections"]
                        if section["start_line"] <= begin <= end <= section["end_line"]
                    ]
                    if not matching:
                        raise ValueError("Quellenbereich liegt außerhalb der zugewiesenen Abschnitte.")
            received[identity] = concept
    if set(seen_groups) != set(expected_groups):
        raise ValueError("Detailplanung enthält nicht alle Batches.")
    if set(received) != set(expected):
        raise ValueError("Detailplanung enthält nicht alle Konzepte.")
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
    origin = f"Abschnitt {section_id!r}, Konzept {index}"
    title = raw.get("title")
    if title is None:
        title = raw.get("name")
    if not isinstance(title, str) or not title.strip():
        raise ValueError(f"{origin}: title muss ein nichtleerer String sein.")
    title = title.strip()
    if len(title) > 500:
        raise ValueError(
            f"{origin}: title enthält {len(title)} Zeichen (maximal 500)."
        )

    description = raw.get("description")
    if description is None:
        description = raw.get("summary")
    if not isinstance(description, str) or not description.strip():
        raise ValueError(
            f"{origin}: description muss ein nichtleerer String sein."
        )
    description = description.strip()
    if len(description) > 4000:
        raise ValueError(
            f"{origin}: description enthält {len(description)} Zeichen "
            "(maximal 4000)."
        )

    tags = raw.get("tags")
    if tags is None:
        tags = []
    elif isinstance(tags, str):
        tags = [tags]
    elif not isinstance(tags, list):
        raise ValueError(
            f"{origin}: tags muss eine Liste von Strings sein."
        )
    if len(tags) > 40:
        raise ValueError(
            f"{origin}: tags enthält {len(tags)} Einträge (maximal 40)."
        )
    normalized: list[str] = []
    seen: set[str] = set()
    for tag_index, value in enumerate(tags, start=1):
        if not isinstance(value, str):
            raise ValueError(
                f"{origin}: tags[{tag_index}] muss ein String sein."
            )
        tag = value.strip()
        if not tag:
            continue
        if len(tag) > 160:
            raise ValueError(
                f"{origin}: tags[{tag_index}] enthält {len(tag)} Zeichen "
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
        raise ValueError("Abschnitts-Outlines fehlen oder sind zu zahlreich.")
    expected = {}
    for task in task_list:
        task = _obj(task, "tasks[]")
        tid = task.get("id")
        if not isinstance(tid, str) or tid in expected:
            raise ValueError("Doppelte oder ungültige Abschnitts-ID.")
        expected[tid] = task
    merged: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    warnings: list[str] = []
    for iteration in iteration_list:
        envelope = _obj(iteration, "outlines.iterations[]")
        tid = envelope.get("id")
        if tid not in expected or tid in seen:
            raise ValueError("Unbekannte oder doppelte Abschnitts-Iteration.")
        seen.add(tid)
        output = _obj(envelope.get("output"), "outlines.iterations[].output")
        if output.get("status") != "ok" or output.get("section_id") != tid:
            raise ValueError("Abschnitts-Outline ist unvollständig oder inkonsistent.")
        local = _array(output.get("concepts"), "outlines.concepts")
        if len(local) > 100:
            raise ValueError("Abschnitts-Outline enthält zu viele Konzepte.")
        for concept_index, entry in enumerate(local, start=1):
            concept = _obj(entry, f"outlines[{tid}].concepts[{concept_index}]")
            key = _slug(
                concept.get("topic_key"),
                f"Abschnitt {tid!r}, Konzept {concept_index}: topic_key",
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
        raise ValueError("Abschnitts-Outlines fehlen oder enthalten keine Konzepte.")
    result = {
        "status": "ok", "source_path": source_path,
        "folder": {
            "id": source_slug, "path": f"doctrines/{source_slug}",
            "title": source_slug, "description": "Doktrin und ihre Konzepte",
        },
        "folders": [],
        "summary": "Zusammenführung der vollständigen Abschnitts-Outlines",
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
        raise ValueError("Abschnittsanalysen fehlen oder sind inkonsistent.")
    if direct:
        return {"tasks": [{"id": "direct", "direct": True, "source_path": source_path}]}
    indexed = {}
    for envelope in iterations:
        envelope = _obj(envelope, "analyses.iterations[]")
        aid = envelope.get("id")
        if not isinstance(aid, str) or aid in indexed:
            raise ValueError("Ungültige oder doppelte Analyse-ID.")
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
            raise ValueError("Unvollständige oder widersprüchliche Abschnittsanalyse.")
        tasks.append({"id": sid, "direct": False, "source_path": source_path,
                      "analysis": analysis})
    if set(indexed) != {x["id"] for x in tasks}:
        raise ValueError("Unerwartete Abschnittsanalyse.")
    return {"tasks": tasks}
