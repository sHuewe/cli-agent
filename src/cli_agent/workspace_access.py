from __future__ import annotations

WORKSPACE_ACCESS_ENV = "CLI_AGENT_WORKSPACE_ACCESS"
WORKSPACE_DIRECTORY_ENV = "CLI_AGENT_WORKSPACE_DIRECTORY"
WORKSPACE_ACCESS_VALUES = frozenset({"none", "read", "write"})
_RESERVED_WORKSPACE_ENV_NAMES = frozenset(
    {
        WORKSPACE_ACCESS_ENV.casefold(),
        WORKSPACE_DIRECTORY_ENV.casefold(),
    }
)
_WORKSPACE_ACCESS_LEVEL = {"none": 0, "read": 1, "write": 2}


def normalize_workspace_access(value: str | None) -> str:
    if value is None:
        return "none"
    if value not in WORKSPACE_ACCESS_VALUES:
        raise ValueError(
            "workspace_access muss 'none', 'read' oder 'write' sein."
        )
    return value


def workspace_access_allows(granted: str, required: str) -> bool:
    granted_value = normalize_workspace_access(granted)
    required_value = normalize_workspace_access(required)
    return _WORKSPACE_ACCESS_LEVEL[granted_value] >= _WORKSPACE_ACCESS_LEVEL[required_value]


def is_reserved_workspace_environment_name(name: str) -> bool:
    return name.casefold() in _RESERVED_WORKSPACE_ENV_NAMES
