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
        literal: list[str] = []
        variables: list[str] = []
        seen_variables: set[str] = set()
        position = 0

        def flush_literal() -> None:
            if literal:
                parts.append("".join(literal))
                literal.clear()

        while position < len(content):
            if content.startswith(_ESCAPED_VARIABLE_PREFIX, position):
                name_start = position + len(_ESCAPED_VARIABLE_PREFIX)
                name_end = content.find(_VARIABLE_SUFFIX, name_start)
                if name_end < 0:
                    raise ValueError(
                        "Unvollständiger escaped Prompt-Template-Platzhalter."
                    )
                name = content[name_start:name_end]
                _validate_variable_name(name)
                literal.append(f"{{{{var:{name}}}}}")
                position = name_end + len(_VARIABLE_SUFFIX)
                continue

            if content.startswith(_VARIABLE_PREFIX, position):
                name_start = position + len(_VARIABLE_PREFIX)
                name_end = content.find(_VARIABLE_SUFFIX, name_start)
                if name_end < 0:
                    raise ValueError(
                        "Unvollständiger Prompt-Template-Platzhalter."
                    )
                name = content[name_start:name_end]
                _validate_variable_name(name)
                flush_literal()
                parts.append(_VariablePart(name))
                if name not in seen_variables:
                    seen_variables.add(name)
                    variables.append(name)
                position = name_end + len(_VARIABLE_SUFFIX)
                continue

            literal.append(content[position])
            position += 1

        flush_literal()
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
