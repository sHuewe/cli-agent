from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePath, PureWindowsPath


class WorkspacePathError(RuntimeError):
    """A path cannot be accessed safely below the configured workspace."""


@dataclass(frozen=True)
class WorkspaceRoot:
    """Resolve untrusted, workspace-relative paths without escaping the root.

    This contains the reusable security boundary shared conceptually with the
    OS MCP server. Domain-specific operations belong in separate classes.
    """

    directory: Path

    @classmethod
    def from_directory(cls, directory: Path) -> WorkspaceRoot:
        try:
            resolved = directory.expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise WorkspacePathError(
                f"Workspace konnte nicht aufgelöst werden: {directory}"
            ) from exc
        if not resolved.is_dir():
            raise WorkspacePathError(
                f"Workspace existiert nicht oder ist kein Ordner: {resolved}"
            )
        return cls(directory=resolved)

    def resolve(self, path: str, *, must_exist: bool = True) -> Path:
        if not isinstance(path, str) or not path.strip():
            raise WorkspacePathError("Der Pfad darf nicht leer sein.")

        raw_path = path.strip()
        candidate = Path(raw_path)
        windows_path = PureWindowsPath(raw_path)
        if candidate.is_absolute() or windows_path.is_absolute() or windows_path.drive:
            raise WorkspacePathError(
                "Der Pfad muss relativ zum konfigurierten Workspace sein."
            )

        # Check before resolving because resolve() would normalize '..' away.
        if ".." in PurePath(raw_path).parts or ".." in windows_path.parts:
            raise WorkspacePathError("Der Pfad darf '..' nicht enthalten.")

        try:
            resolved = (self.directory / candidate).resolve(strict=must_exist)
        except (OSError, RuntimeError, ValueError) as exc:
            raise WorkspacePathError(
                f"Pfad konnte nicht aufgelöst werden: {path!r}"
            ) from exc

        try:
            resolved.relative_to(self.directory)
        except ValueError as exc:
            # This also blocks symlinks that point outside the workspace.
            raise WorkspacePathError(
                "Der Pfad verweist außerhalb des konfigurierten Workspaces."
            ) from exc
        return resolved

    def relative(self, path: Path) -> str:
        try:
            return path.relative_to(self.directory).as_posix() or "."
        except ValueError as exc:
            raise WorkspacePathError(
                "Der Pfad verweist außerhalb des konfigurierten Workspaces."
            ) from exc
