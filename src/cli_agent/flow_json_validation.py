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


def validate_okf_plan(value: Any, *, plan_kind: str) -> dict[str, Any]:
    """Return the original plan after bounded, structural path validation."""
    plan = _obj(value, "OKF-Plan")
    if plan.get("status") != "ok":
        raise ValueError("OKF-Plan ist nicht erfolgreich; kein Schreibschritt erlaubt.")
    if plan_kind == "doctrine":
        _validate_doctrine(plan)
    elif plan_kind == "themes":
        _validate_themes(plan)
    else:
        raise ValueError(f"Unbekannte OKF-Planart: {plan_kind!r}.")
    return plan
