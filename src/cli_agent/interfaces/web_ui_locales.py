"""Runtime-discovered, packaged browser translation catalogs.

A new valid static/i18n/<language>.json file becomes available without
modifying Python routes or JavaScript. Only files in this package directory
are exposed; locale names are strictly validated.
"""
from __future__ import annotations

import json
import re
from importlib.resources import files

_LOCALE_NAME = re.compile(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})?\Z")
_MAX_CATALOG_BYTES = 256 * 1024


def _catalog_dir():
    return files("cli_agent.interfaces").joinpath("static", "i18n")


def _read_locale(code: str) -> dict[str, str] | None:
    if not isinstance(code, str) or not _LOCALE_NAME.fullmatch(code):
        return None
    resource = _catalog_dir().joinpath(code + ".json")
    if not resource.is_file():
        return None
    raw = resource.read_bytes()
    if len(raw) > _MAX_CATALOG_BYTES:
        return None
    try:
        catalog = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError):
        return None
    if not isinstance(catalog, dict) or not catalog:
        return None
    if any(not isinstance(k, str) or not isinstance(v, str) for k, v in catalog.items()):
        return None
    return catalog


def available_locales() -> list[str]:
    """Return supported languages from valid JSON files, without a hardcoded list."""
    codes = []
    for resource in _catalog_dir().iterdir():
        name = resource.name
        if not name.endswith(".json"):
            continue
        code = name[:-5]
        if _read_locale(code) is not None:
            codes.append(code)
    return sorted(set(codes), key=lambda name: (name != "en", name))


def get_locale(code: str) -> dict[str, str] | None:
    """Return only valid language catalogs, rejecting arbitrary paths."""
    return _read_locale(code)
