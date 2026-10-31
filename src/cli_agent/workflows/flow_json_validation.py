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
