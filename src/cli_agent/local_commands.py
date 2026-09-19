from __future__ import annotations

from dataclasses import dataclass
from difflib import get_close_matches


@dataclass(frozen=True)
class LocalCommandSpec:
    name: str
    usage: str
    argument_count: int
    fuzzy_suggestion: bool = False


@dataclass(frozen=True)
class LocalCommandInput:
    is_local: bool
    command: str | None = None
    arguments: tuple[str, ...] = ()
    error: str | None = None


_LOCAL_COMMAND_SPECS = (
    LocalCommandSpec("tokens", "tokens", 0, fuzzy_suggestion=True),
    LocalCommandSpec(
        "add_web_context",
        "add_web_context <URL>",
        1,
        fuzzy_suggestion=True,
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
_FUZZY_CUTOFF = 0.82


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

    # Long underscore/hyphen commands are highly distinctive.  For the short
    # "tokens" command, only a single-token input is considered command-like.
    if "_" in command_name or "-" in command_name:
        candidates = tuple(
            name for name in _FUZZY_COMMAND_NAMES if "_" in name
        )
    elif not arguments:
        candidates = ("tokens",)
    else:
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
    return LocalCommandInput(
        is_local=True,
        error=(
            f"Unbekannter lokaler Befehl {entered!r}. "
            f"Meintest du: {suggested.usage}?"
        ),
    )
