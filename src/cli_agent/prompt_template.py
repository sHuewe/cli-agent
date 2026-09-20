from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

_VARIABLE_PREFIX = "{{var:"
_ESCAPED_VARIABLE_PREFIX = r"\{{var:"
_VARIABLE_SUFFIX = "}}"
_VARIABLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*\Z")


@dataclass(frozen=True)
class _VariablePart:
    name: str


@dataclass(frozen=True)
class PromptTemplate:
    """Small, deterministic prompt template with explicit user variables."""

    parts: tuple[str | _VariablePart, ...]
    variables: tuple[str, ...]

    @classmethod
    def parse(cls, content: str) -> PromptTemplate:
        parts: list[str | _VariablePart] = []
        variables: list[str] = []
        seen_variables: set[str] = set()
        position = 0

        def append_literal(value: str) -> None:
            if not value:
                return
            if parts and isinstance(parts[-1], str):
                parts[-1] += value
            else:
                parts.append(value)

        while position < len(content):
            escaped_position = content.find(_ESCAPED_VARIABLE_PREFIX, position)
            variable_position = content.find(_VARIABLE_PREFIX, position)

            if escaped_position < 0 and variable_position < 0:
                append_literal(content[position:])
                break

            is_escaped = escaped_position >= 0 and (
                variable_position < 0 or escaped_position < variable_position
            )
            marker_position = (
                escaped_position if is_escaped else variable_position
            )
            append_literal(content[position:marker_position])

            prefix = (
                _ESCAPED_VARIABLE_PREFIX if is_escaped else _VARIABLE_PREFIX
            )
            name_start = marker_position + len(prefix)
            name_end = content.find(_VARIABLE_SUFFIX, name_start)
            if name_end < 0:
                kind = "escaped " if is_escaped else ""
                raise ValueError(
                    f"Unvollständiger {kind}Prompt-Template-Platzhalter."
                )

            name = content[name_start:name_end]
            _validate_variable_name(name)

            if is_escaped:
                append_literal(f"{{{{var:{name}}}}}")
            else:
                parts.append(_VariablePart(name))
                if name not in seen_variables:
                    seen_variables.add(name)
                    variables.append(name)

            position = name_end + len(_VARIABLE_SUFFIX)

        return cls(parts=tuple(parts), variables=tuple(variables))

    def render(self, values: dict[str, str]) -> str:
        required = set(self.variables)
        unknown = sorted(set(values) - required)
        if unknown:
            raise ValueError(
                "Unbekannte Prompt-Template-Variable(n): "
                + ", ".join(unknown)
            )

        missing = [name for name in self.variables if name not in values]
        if missing:
            raise ValueError(
                "Fehlende Prompt-Template-Variable(n): "
                + ", ".join(missing)
            )

        rendered: list[str] = []
        for part in self.parts:
            if isinstance(part, _VariablePart):
                rendered.append(values[part.name])
            else:
                rendered.append(part)
        return "".join(rendered)


def parse_variable_assignments(assignments: Iterable[str]) -> dict[str, str]:
    """Parse repeated NAME=VALUE CLI assignments without interpreting VALUE."""

    values: dict[str, str] = {}
    for assignment in assignments:
        if "=" not in assignment:
            raise ValueError(
                f"Ungültige --var-Angabe {assignment!r}; erwartet wird NAME=WERT."
            )
        name, value = assignment.split("=", 1)
        _validate_variable_name(name)
        if name in values:
            raise ValueError(
                f"Prompt-Template-Variable {name!r} wurde mehrfach mit --var gesetzt."
            )
        values[name] = value
    return values


def _validate_variable_name(name: str) -> None:
    if not _VARIABLE_NAME.fullmatch(name):
        raise ValueError(
            f"Ungültiger Prompt-Template-Variablenname {name!r}; erlaubt sind "
            "ASCII-Buchstaben, Ziffern, '_' und '-' und der Name muss mit "
            "einem Buchstaben oder '_' beginnen."
        )
