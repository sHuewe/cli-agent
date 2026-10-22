"""Deterministic, bounded assembly and coverage checks for JSON flow artifacts."""

from __future__ import annotations

import re
from typing import Any, Callable

MAX_KNOWLEDGE_ITEMS = 100_000
MAX_CONCEPTS = 1_000

# Windows treats these as device names even with extensions (e.g. con.md).
# Checking individual path segments keeps generated OKF paths portable.
_WINDOWS_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"{prefix}{index}" for prefix in ("com", "lpt") for index in range(1, 10)}
)


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} muss ein JSON-Objekt sein.")
    return value


def _list(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{name} muss eine JSON-Liste sein.")
    return value


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} muss ein nichtleerer String sein.")
    return value


def group_assigned_items(
    *,
    routes: Any,
    assignments: Any,
    topic_plan: Any,
    max_concept_bytes: int,
    serializer: Callable[[Any], str],
) -> dict[str, Any]:
    """Attach original routed objects to globally agreed topics without an LLM.

    Route objects are the sole source of knowledge-item content. LLM assignment
    artifacts contain only item IDs and canonical topic IDs. Every item must be
    assigned exactly once; duplicates, omissions and unknown IDs fail closed.
    """
    route_list = _list(routes, "routes")
    assignment_batches = _list(assignments, "assignments")
    registry = _object(topic_plan, "topics")
    folder = _object(registry.get("folder"), "topics.folder")
    folder_id = _string(folder.get("id"), "topics.folder.id")
    folder_path = _string(folder.get("path"), "topics.folder.path")
    # OKF module folders may be nested (e.g. doctrines/<source-slug>).
    # Validate every segment: this rejects absolute paths, traversal,
    # backslashes, empty segments, and unsafe or platform-dependent names.
    folder_segment = r"[a-z0-9]+(?:-[a-z0-9]+)*"
    if (
        len(folder_path) > 512
        or not re.fullmatch(rf"{folder_segment}(?:/{folder_segment})*", folder_path)
        or any(segment in _WINDOWS_RESERVED_NAMES for segment in folder_path.split("/"))
    ):
        raise ValueError("Ungültiger semantischer Ordnerpfad.")
    if type(max_concept_bytes) is not int or not 1024 <= max_concept_bytes <= 10_000_000:
        raise ValueError("Ungültiges Konzept-Bytelimit.")

    topic_map: dict[str, dict[str, Any]] = {}
    paths: set[str] = set()
    for source in _list(registry.get("topics"), "topics.topics"):
        topic = _object(source, "topic")
        topic_id = _string(topic.get("id"), "topic.id")
        path = _string(topic.get("path"), "topic.path")
        if not re.fullmatch(r"[a-z][a-z0-9_-]*", topic_id):
            raise ValueError(f"Ungültige Topic-ID {topic_id!r}.")
        if not re.fullmatch(
            re.escape(folder_path) + r"/[a-z0-9]+(?:-[a-z0-9]+)*\.md", path
        ):
            raise ValueError(f"Topic-Pfad außerhalb des Ordners: {path!r}.")
        if path.rsplit("/", 1)[-1][:-3] in _WINDOWS_RESERVED_NAMES:
            raise ValueError(f"Reservierter Windows-Gerätename im Topic-Pfad: {path!r}.")
        if topic_id in topic_map or path.casefold() in paths:
            raise ValueError("Topic-IDs und Dateipfade müssen eindeutig sein.")
        for name in ("title", "description", "type"):
            _string(topic.get(name), f"topic.{name}")
        tags = _list(topic.get("tags", []), "topic.tags")
        if not all(isinstance(tag, str) for tag in tags):
            raise ValueError("Topic-Tags müssen Strings sein.")
        paths.add(path.casefold())
        topic_map[topic_id] = topic
    if len(topic_map) > MAX_CONCEPTS:
        raise ValueError("Zu viele globale Topics.")

    sources: dict[str, dict[str, Any]] = {}
    ordered_ids: list[str] = []
    for route_value in route_list:
        route = _object(route_value, "route")
        document_id = _string(route.get("document_id"), "route.document_id")
        _string(route.get("chunk_id"), "route.chunk_id")
        if route.get("folder_id") != folder_id:
            raise ValueError("Routing-Eintrag gehört zum falschen Ordner.")
        for raw_item in _list(route.get("knowledge_items"), "route.knowledge_items"):
            item = _object(raw_item, "knowledge_item")
            local_id = _string(item.get("id"), "knowledge_item.id")
            global_id = f"{document_id}:{local_id}"
            if global_id in sources:
                raise ValueError(f"Doppelte globale Knowledge-Item-ID: {global_id}")
            evidence = _list(item.get("evidence_ranges"), "knowledge_item.evidence_ranges")
            if not all(isinstance(entry, dict) for entry in evidence):
                raise ValueError("Evidence-Ranges müssen JSON-Objekte sein.")
            for field in ("kind", "name", "description"):
                _string(item.get(field), f"knowledge_item.{field}")
            if type(item.get("normative")) is not bool:
                raise ValueError("knowledge_item.normative muss boolesch sein.")
            sources[global_id] = {**item, "id": global_id}
            ordered_ids.append(global_id)
            if len(ordered_ids) > MAX_KNOWLEDGE_ITEMS:
                raise ValueError("Zu viele Knowledge Items.")

    allocated: dict[str, str] = {}
    for batch_value in assignment_batches:
        batch = _object(batch_value, "assignment batch")
        for assignment_value in _list(batch.get("assignments"), "batch.assignments"):
            assignment = _object(assignment_value, "assignment")
            item_id = _string(assignment.get("item_id"), "assignment.item_id")
            topic_id = _string(assignment.get("topic_id"), "assignment.topic_id")
            if item_id in allocated:
                raise ValueError(f"Knowledge Item mehrfach zugewiesen: {item_id}")
            if item_id not in sources:
                raise ValueError(f"Unbekanntes Knowledge Item in Zuweisung: {item_id}")
            if topic_id not in topic_map:
                raise ValueError(f"Unbekannte globale Topic-ID: {topic_id}")
            allocated[item_id] = topic_id
    missing = [item_id for item_id in ordered_ids if item_id not in allocated]
    if missing:
        raise ValueError(
            "Knowledge Items ohne Topic-Zuweisung: " + ", ".join(missing[:10])
        )

    grouped: dict[str, list[dict[str, Any]]] = {key: [] for key in topic_map}
    for item_id in ordered_ids:
        grouped[allocated[item_id]].append(sources[item_id])

    def byte_count(value: Any) -> int:
        return len(serializer(value).encode("utf-8"))

    concepts: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    used_paths: set[str] = set()
    reserved_ids = set(topic_map)
    reserved_paths = paths

    def new_concept(
        topic: dict[str, Any], part: int,
    ) -> tuple[dict[str, Any], int]:
        # Generated part IDs and paths must not shadow a later registry topic.
        # Reserve every declared topic, even topics processed after this one.
        while True:
            topic_id = topic["id"]
            path = topic["path"]
            title = topic["title"]
            if part > 1:
                topic_id += f"-p{part:03d}"
                path = path[:-3] + f"-part-{part}.md"
                title += f" (Teil {part})"
            if (
                topic_id not in used_ids
                and path.casefold() not in used_paths
                and (
                    part == 1
                    or (
                        topic_id not in reserved_ids
                        and path.casefold() not in reserved_paths
                    )
                )
            ):
                return {
                    "id": topic_id,
                    "folder_id": folder_id,
                    "path": path,
                    "type": topic["type"],
                    "title": title,
                    "description": topic["description"],
                    "tags": topic.get("tags", []),
                    "related": [],
                    "evidence_ranges": [],
                    "coverage_items": [],
                }, part
            if part == 1:
                raise ValueError("Kollidierende globale Topic-ID oder Topic-Pfad.")
            part += 1

    def append_concept(concept: dict[str, Any], measured_size: int) -> None:
        # Enforce the count before growing the result (also within one topic).
        if len(concepts) >= MAX_CONCEPTS:
            raise ValueError("Zu viele resultierende Konzepte.")
        concept_id = concept["id"]
        path_key = concept["path"].casefold()
        if concept_id in used_ids or path_key in used_paths:
            raise ValueError("Kollidierende Konzept-ID oder Konzeptpfad.")
        # One final serialization per complete concept validates the running
        # byte counter; it does not re-serialize the growing prefix per item.
        if measured_size != byte_count(concept):
            raise ValueError("Inkonsistente JSON-Konzept-Größenberechnung.")
        if measured_size > max_concept_bytes:
            raise ValueError("Konzept überschreitet das Byte-Limit.")
        used_ids.add(concept_id)
        used_paths.add(path_key)
        concepts.append(concept)

    for topic_id, items in grouped.items():
        if not items:
            continue
        topic = topic_map[topic_id]
        part = 1
        current, part = new_concept(topic, part)
        current_size = byte_count(current)
        evidence_seen: set[str] = set()

        def add_item(
            item: dict[str, Any],
            item_size: int,
            evidence_parts: list[tuple[str, dict[str, Any], int]],
        ) -> bool:
            nonlocal current_size
            new_evidence: list[tuple[str, dict[str, Any]]] = []
            new_keys: set[str] = set()
            # Exact accounting for the compact JSON object: [] already belongs
            # to the base size; each additional array item adds its serialized
            # bytes and one comma, except the first entry in each array.
            candidate_size = (
                current_size + item_size + (1 if current["coverage_items"] else 0)
            )
            for key, entry, encoded_size in evidence_parts:
                if key in evidence_seen or key in new_keys:
                    continue
                candidate_size += encoded_size + (
                    1 if current["evidence_ranges"] or new_evidence else 0
                )
                new_evidence.append((key, entry))
                new_keys.add(key)
            if candidate_size > max_concept_bytes:
                return False
            current["coverage_items"].append(item)
            current["evidence_ranges"].extend(entry for _, entry in new_evidence)
            evidence_seen.update(new_keys)
            current_size = candidate_size
            return True

        for item in items:
            item_size = byte_count(item)
            evidence_parts = [
                (encoded, entry, len(encoded.encode("utf-8")))
                for entry in item["evidence_ranges"]
                for encoded in (serializer(entry),)
            ]
            if not add_item(item, item_size, evidence_parts):
                if not current["coverage_items"]:
                    raise ValueError(
                        f"Knowledge Item {item['id']} überschreitet max_concept_bytes."
                    )
                append_concept(current, current_size)
                current, part = new_concept(topic, part + 1)
                current_size = byte_count(current)
                evidence_seen.clear()
                if not add_item(item, item_size, evidence_parts):
                    raise ValueError(
                        f"Knowledge Item {item['id']} überschreitet max_concept_bytes."
                    )
        append_concept(current, current_size)

    return {
        "folder": folder,
        "concepts": concepts,
        "concept_summaries": [
            {"id": item["id"], "path": item["path"], "title": item["title"]}
            for item in concepts
        ],
        "total_items": len(ordered_ids),
        "warnings": [],
    }


def verify_assigned_coverage(*, plan: Any, results: Any) -> dict[str, Any]:
    """Compare source-grounded planned IDs against completed concept repairs."""
    source = _object(plan, "concept plan")
    folder = _object(source.get("folder"), "concept plan.folder")
    folder_id = _string(folder.get("id"), "concept plan.folder.id")
    expected: dict[str, tuple[str, bool]] = {}
    concept_ids: set[str] = set()
    for raw_concept in _list(source.get("concepts"), "concept plan.concepts"):
        concept = _object(raw_concept, "concept")
        concept_id = _string(concept.get("id"), "concept.id")
        if concept_id in concept_ids:
            raise ValueError("Doppelte Konzept-ID im Gruppenplan.")
        concept_ids.add(concept_id)
        for value in _list(concept.get("coverage_items"), "concept.coverage_items"):
            item = _object(value, "coverage item")
            item_id = _string(item.get("id"), "coverage item.id")
            if item_id in expected:
                raise ValueError(f"Doppelte erwartete Knowledge-Item-ID: {item_id}")
            if type(item.get("normative")) is not bool:
                raise ValueError("coverage_item.normative muss boolesch sein.")
            expected[item_id] = (concept_id, item["normative"])

    documented: set[str] = set()
    seen_results: set[str] = set()
    warnings: list[str] = []
    for raw_result in _list(results, "concept results"):
        result = _object(raw_result, "concept result")
        concept_id = _string(result.get("concept_id"), "concept result.concept_id")
        if concept_id not in concept_ids or concept_id in seen_results:
            raise ValueError(f"Ungültiges oder doppeltes Konzeptergebnis: {concept_id}")
        seen_results.add(concept_id)
        if result.get("status") not in {"ok", "repaired"}:
            warnings.append(f"Konzept {concept_id}: Reparatur nicht abgeschlossen.")
        remaining = _list(result.get("remaining_item_ids", []), "remaining_item_ids")
        if remaining:
            warnings.append(f"Konzept {concept_id}: offene Knowledge Items.")
        if _list(result.get("remaining_warnings", []), "remaining_warnings"):
            warnings.append(f"Konzept {concept_id}: offene Warnungen.")
        for raw_id in _list(result.get("documented_item_ids"), "documented_item_ids"):
            item_id = _string(raw_id, "documented_item_id")
            if item_id not in expected or expected[item_id][0] != concept_id:
                warnings.append(f"Unerwartet dokumentiertes Knowledge Item: {item_id}")
            else:
                documented.add(item_id)
    if seen_results != concept_ids:
        warnings.append("Nicht alle geplanten Konzepte haben ein Reparaturergebnis.")
    missing = sorted(expected.keys() - documented)
    expected_norm = {key for key, (_, normative) in expected.items() if normative}
    expected_desc = set(expected) - expected_norm
    return {
        "status": "ok" if not missing and not warnings else "needs_attention",
        "folder_id": folder_id,
        "expected_normative_items": len(expected_norm),
        "documented_normative_items": len(documented & expected_norm),
        "expected_descriptive_items": len(expected_desc),
        "documented_descriptive_items": len(documented & expected_desc),
        "missing_item_ids": missing,
        "warnings": warnings,
    }
