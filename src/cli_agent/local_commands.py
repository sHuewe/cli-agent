from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from difflib import get_close_matches
from urllib.parse import urlsplit


@dataclass(frozen=True)
class LocalCommandSpec:
    name: str
    usage: str
    argument_count: int
    fuzzy_suggestion: bool = False
    fuzzy_argument_validator: Callable[[tuple[str, ...]], bool] | None = None


@dataclass(frozen=True)
class LocalCommandInput:
    is_local: bool
    command: str | None = None
    arguments: tuple[str, ...] = ()
    error: str | None = None


def _looks_like_web_context_arguments(arguments: tuple[str, ...]) -> bool:
    if len(arguments) != 1:
        return False
    parsed = urlsplit(arguments[0])
    return parsed.scheme.casefold() in {"http", "https"} and bool(parsed.netloc)


_LOCAL_COMMAND_SPECS = (
    LocalCommandSpec("tokens", "tokens", 0),
    LocalCommandSpec(
        "add_web_context",
        "add_web_context <URL>",
        1,
        fuzzy_suggestion=True,
        fuzzy_argument_validator=_looks_like_web_context_arguments,
    ),
    LocalCommandSpec(
        "clear_web_context",
        "clear_web_context",
        0,
        fuzzy_suggestion=True,
    ),
    LocalCommandSpec("enable", "enable <SERVER>", 1),
    LocalCommandSpec("disable", "disable <SERVER>", 1),
)
_LOCAL_COMMAND_BY_NAME = {spec.name: spec for spec in _LOCAL_COMMAND_SPECS}
_FUZZY_COMMAND_NAMES = tuple(
    spec.name for spec in _LOCAL_COMMAND_SPECS if spec.fuzzy_suggestion
)
_FUZZY_CUTOFF = 0.93


def classify_local_command(prompt: str) -> LocalCommandInput:
    """Classify exact local commands and conservative likely command typos.

    Fuzzy matching is intentionally limited to distinctive local commands.  A
    typo is never auto-corrected or executed; the caller only returns a local
    error/suggestion instead of spending an LLM request on the input.
    """

    stripped = prompt.strip()
    if not stripped:
        return LocalCommandInput(is_local=False)

    parts = stripped.split()
    entered = parts[0]
    command_name = entered.casefold()
    arguments = tuple(parts[1:])

    spec = _LOCAL_COMMAND_BY_NAME.get(command_name)
    if spec is not None:
        if len(arguments) != spec.argument_count:
            return LocalCommandInput(
                is_local=True,
                command=spec.name,
                arguments=arguments,
                error=(
                    f"Ungültige Syntax für lokalen Befehl {spec.name!r}. "
                    f"Verwendung: {spec.usage}"
                ),
            )
        return LocalCommandInput(
            is_local=True,
            command=spec.name,
            arguments=arguments,
        )

    # Fuzzy matching is deliberately limited to distinctive command-shaped
    # names. Short ordinary words such as "tokens", "enable" and "disable"
    # would otherwise create too many false positives in normal prompts.
    if "_" not in command_name and "-" not in command_name:
        return LocalCommandInput(is_local=False)

    # Require the command-family prefix as well as a high SequenceMatcher
    # similarity. This avoids swallowing valid technical prompts such as
    # "web_context usage in Python" or identifiers like "add_web_contextual".
    prefix = command_name.split("_", 1)[0].split("-", 1)[0]
    candidates = tuple(
        name
        for name in _FUZZY_COMMAND_NAMES
        if name.startswith(prefix + "_")
    )
    if not candidates:
        return LocalCommandInput(is_local=False)

    matches = get_close_matches(
        command_name,
        candidates,
        n=2,
        cutoff=_FUZZY_CUTOFF,
    )
    if len(matches) != 1:
        return LocalCommandInput(is_local=False)

    suggested = _LOCAL_COMMAND_BY_NAME[matches[0]]

    # A bare near-match is very likely an incomplete local command. If more
    # tokens follow, only intercept the input when they also have the expected
    # invocation shape. This keeps prose/code references such as
    # "add_web_contexts usage in Python" or "add_web_context() usage" on the
    # normal LLM path.
    if arguments:
        validator = suggested.fuzzy_argument_validator
        if validator is None:
            if len(arguments) != suggested.argument_count:
                return LocalCommandInput(is_local=False)
        elif not validator(arguments):
            return LocalCommandInput(is_local=False)

    return LocalCommandInput(
        is_local=True,
        error=(
            f"Unbekannter lokaler Befehl {entered!r}. "
            f"Meintest du: {suggested.usage}?"
        ),
    )
