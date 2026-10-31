from __future__ import annotations

import io
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

_VARIABLE_PREFIX = "{{var:"
_VARIABLE_SUFFIX = "}}"
_VARIABLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*\Z")

# These are last-resort resource bounds, not normal usability limits.
# A real-world prompt should be many orders of magnitude below the occurrence
# limit. Keeping the rendered byte bound aligned with the prompt-file input
# bound prevents variable expansion from bypassing that memory/DoS envelope.
MAX_PROMPT_TEMPLATE_PLACEHOLDERS = 1_000_000
MAX_RENDERED_PROMPT_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True)
class PromptTemplate:
    """Small, deterministic prompt template with explicit user variables."""

    source: str
    variables: tuple[str, ...]
    has_template_syntax: bool

    @classmethod
    def parse(cls, content: str) -> PromptTemplate:
        if content.find(_VARIABLE_PREFIX) < 0:
            return cls(
                source=content,
                variables=(),
                has_template_syntax=False,
            )

        variables: list[str] = []
        seen_variables: set[str] = set()
        for is_variable, value in _iter_template_segments(content):
            if not is_variable or value in seen_variables:
                continue
            seen_variables.add(value)
            variables.append(value)

        return cls(
            source=content,
            variables=tuple(variables),
            has_template_syntax=True,
        )

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

        if not self.has_template_syntax:
            return self.source

        rendered = io.StringIO()
        rendered_bytes = 0
        variable_sizes = {
            name: len(value.encode("utf-8", errors="surrogatepass"))
            for name, value in values.items()
        }

        for is_variable, value in _iter_template_segments(self.source):
            text = values[value] if is_variable else value
            size = (
                variable_sizes[value]
                if is_variable
                else len(text.encode("utf-8", errors="surrogatepass"))
            )
            rendered_bytes += size
            if rendered_bytes > MAX_RENDERED_PROMPT_BYTES:
                raise ValueError(
                    "Gerenderter Prompt überschreitet das Sicherheitslimit von "
                    f"{MAX_RENDERED_PROMPT_BYTES} UTF-8-Bytes."
                )
            rendered.write(text)

        return rendered.getvalue()


def _iter_template_segments(content: str) -> Iterator[tuple[bool, str]]:
    """Yield literal spans / variable names in one forward scan.

    The iterator never retains one object per placeholder. Escaped placeholders
    are emitted as literal placeholder text.
    """

    position = 0
    marker_position = content.find(_VARIABLE_PREFIX)
    placeholder_count = 0

    while marker_position >= 0:
        placeholder_count += 1
        if placeholder_count > MAX_PROMPT_TEMPLATE_PLACEHOLDERS:
            raise ValueError(
                "Prompt-Template überschreitet das Sicherheitslimit von "
                f"{MAX_PROMPT_TEMPLATE_PLACEHOLDERS} Platzhaltern."
            )

        is_escaped = (
            marker_position > position
            and content[marker_position - 1] == "\\"
        )
        literal_end = marker_position - 1 if is_escaped else marker_position
        if literal_end > position:
            yield False, content[position:literal_end]

        name_start = marker_position + len(_VARIABLE_PREFIX)
        name_end = content.find(_VARIABLE_SUFFIX, name_start)
        if name_end < 0:
            kind = "escaped " if is_escaped else ""
            raise ValueError(
                f"Unvollständiger {kind}Prompt-Template-Platzhalter."
            )

        name = content[name_start:name_end]
        _validate_variable_name(name)

        if is_escaped:
            # Consume exactly one escape backslash. Any preceding backslashes
            # remain part of the literal span above.
            yield False, f"{{{{var:{name}}}}}"
        else:
            yield True, name

        position = name_end + len(_VARIABLE_SUFFIX)
        marker_position = content.find(_VARIABLE_PREFIX, position)

    if position < len(content):
        yield False, content[position:]


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
