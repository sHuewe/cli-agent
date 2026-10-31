"""Public Workspace security policy and filesystem operation facade."""
from __future__ import annotations
from cli_agent.platform.workspace_core import *
from cli_agent.platform.workspace_io import WorkspaceIoMixin

@dataclass(frozen=True)
class Workspace(WorkspaceIoMixin):
    directory: Path
    config: McpServerConfig
    protected_paths: frozenset[Path] = frozenset()
    mutation_protected_paths: frozenset[Path] = frozenset()
    allowed_paths: tuple[AllowedPath, ...] = ()

    @classmethod
    def from_directory(
        cls,
        directory: Path,
        config: McpServerConfig,
        *,
        protected_paths: tuple[Path, ...] = (),
        mutation_protected_paths: tuple[Path, ...] = (),
        allowed_paths: tuple[AllowedPath, ...] = (),
    ) -> Workspace:
        resolved = directory.resolve()
        if not resolved.is_dir():
            raise WorkspaceError(f"Projekt-Workspace existiert nicht: {resolved}")

        def collect_workspace_paths(paths: tuple[Path, ...]) -> frozenset[Path]:
            collected: set[Path] = set()
            for path in paths:
                protected_path = path.expanduser().resolve(strict=False)
                try:
                    protected_path.relative_to(resolved)
                except ValueError:
                    # Paths outside the workspace are unreachable through this
                    # server and therefore need no additional protection here.
                    continue
                collected.add(protected_path)
            return frozenset(collected)

        checked_allowed: list[AllowedPath] = []
        for item in allowed_paths:
            if not isinstance(item, AllowedPath) or item.access not in {"read", "write"}:
                raise WorkspaceError("allowed_paths benötigt AllowedPath mit access='read' oder 'write'.")
            raw = item.path.expanduser()
            if ".." in raw.parts or ".." in PureWindowsPath(str(raw)).parts:
                raise WorkspaceError("allowed_paths darf '..' nicht enthalten.")
            candidate = raw if raw.is_absolute() else resolved / raw
            lexical = candidate.absolute()
            try:
                relative = lexical.relative_to(resolved)
            except ValueError as exc:
                raise WorkspaceError("allowed_paths muss innerhalb des Workspace liegen.") from exc
            if not relative.parts:
                raise WorkspaceError("Der gesamte Workspace darf nicht freigegeben werden.")
            try:
                canonical = candidate.resolve(strict=False)
            except (OSError, RuntimeError) as exc:
                raise WorkspaceError("allowed_paths konnte nicht sicher aufgelöst werden.") from exc
            if os.path.normcase(str(lexical)) != os.path.normcase(str(canonical)):
                raise WorkspaceError("Symlinks oder Junctions in allowed_paths sind nicht erlaubt.")
            checked_allowed.append(AllowedPath(canonical, item.access))

        return cls(
            directory=resolved,
            config=config,
            protected_paths=collect_workspace_paths(protected_paths),
            mutation_protected_paths=collect_workspace_paths(
                mutation_protected_paths
            ),
            allowed_paths=tuple(checked_allowed),
        )

    def resolve_path(self, path: str, *, must_exist: bool = True) -> Path:
        if not isinstance(path, str) or not path.strip():
            raise WorkspaceError("Der Pfad darf nicht leer sein.")

        raw_path = path.strip()
        candidate = Path(raw_path)
        windows_path = PureWindowsPath(raw_path)
        if (
            candidate.is_absolute()
            or windows_path.is_absolute()
            or windows_path.drive
            or windows_path.root
        ):
            raise WorkspaceError("Der Pfad muss relativ zum Projekt-Workspace sein.")

        # Check the lexical path before resolving it. Resolving first would
        # normalize ".." away and make the explicit prohibition ineffective.
        if ".." in PurePath(raw_path).parts or ".." in windows_path.parts:
            raise WorkspaceError("Der Pfad darf '..' nicht enthalten.")

        resolved = (self.directory / candidate).resolve(strict=must_exist)
        try:
            resolved.relative_to(self.directory)
        except ValueError as exc:
            # Also blocks symlinks that point outside the workspace.
            raise WorkspaceError(
                "Der Pfad verweist außerhalb des Projekt-Workspaces."
            ) from exc

        return resolved

    def resolve_direct_path(self, path: str, *, must_exist: bool = True) -> Path:
        """Resolve a path but reject symlink/junction indirection.

        Operations that must act on the path the caller named rather than on a
        canonicalized target use this stricter resolver. Comparing the lexical
        absolute path with the canonical path also rejects indirection in parent
        components.
        """
        resolved = self.resolve_path(path, must_exist=must_exist)
        lexical = (self.directory / Path(path.strip())).absolute()
        if os.path.normcase(str(lexical)) != os.path.normcase(str(resolved)):
            raise WorkspaceError(
                "Symlinks oder Junctions sind für diese Dateioperation nicht erlaubt."
            )
        return resolved

    @staticmethod
    def _is_text_file(path: Path) -> bool:
        name = path.name.lower()
        return name in TEXT_FILENAMES or path.suffix.lower() in TEXT_SUFFIXES

    @staticmethod
    def _is_sensitive_file(
        path: Path,
        *,
        workspace: Path | None = None,
    ) -> bool:
        name = path.name.casefold()
        path_parts = path.parts
        if workspace is not None:
            try:
                relative_parts = path.relative_to(workspace).parts
            except ValueError:
                # Callers are expected to enforce workspace containment first.
                # If that invariant is violated, keep the sensitivity check
                # fail-closed instead of classifying an external path as safe.
                return True
            # Ignore sensitive ancestors above the selected workspace, but keep
            # the workspace root itself in scope. A workspace explicitly named
            # "secrets", "credentials", ".git", etc. remains protected.
            path_parts = (workspace.name, *relative_parts)
        return (
            any(part.casefold() in SENSITIVE_DIRECTORY_NAMES for part in path_parts)
            or name.startswith(".env")
            or name in SENSITIVE_FILENAMES
            or path.suffix.casefold() in SENSITIVE_SUFFIXES
            or ".log." in name
            or name.endswith(".log")
        )

    def _matching_allowed_path(self, resolved: Path) -> AllowedPath | None:
        matches = (
            item for item in self.allowed_paths
            if resolved == item.path or item.path in resolved.parents
        )
        # Prefer the narrowest exception, and read-only on an equal path.
        return next(iter(sorted(
            matches, key=lambda item: (-len(item.path.parts), item.access != "read")
        )), None)

    def _is_protected_path(self, path: Path) -> bool:
        try:
            # Reject indirection even for paths below a permitted exception.
            if path_entry_is_symlink_or_reparse(path):
                return True
            resolved = path.resolve(strict=False)
            if os.path.normcase(str(path.absolute())) != os.path.normcase(str(resolved)):
                return True
        except (OSError, RuntimeError):
            return True

        # Explicit exclusions always win over an allowed-path exception.
        # Only built-in sensitivity of ancestor directories is overridden.
        if any(resolved == protected or protected in resolved.parents
               for protected in self.protected_paths):
            return True
        allowed = self._matching_allowed_path(resolved)
        if allowed is not None:
            return self._is_sensitive_file(resolved, workspace=allowed.path)

        return (
            self._is_sensitive_file(resolved, workspace=self.directory)
            or any(
                resolved == protected or protected in resolved.parents
                for protected in self.protected_paths
            )
        )

    def _reject_protected_read(self, path: Path) -> None:
        if self._is_protected_path(path):
            raise WorkspaceError(
                "Das Lesen von Secret-/Credential- oder geschützten "
                "Agent-Dateien ist über den Workspace-Dateizugriff nicht erlaubt."
            )

    def _is_mutation_protected_path(self, path: Path) -> bool:
        if self._is_protected_path(path):
            return True
        allowed = self._matching_allowed_path(path.resolve(strict=False))
        if allowed is not None and allowed.access != "write":
            return True
        try:
            if path_entry_is_symlink_or_reparse(path):
                return True
            resolved = path.resolve(strict=False)
        except (OSError, RuntimeError):
            return True
        return any(
            resolved == protected or protected in resolved.parents
            for protected in self.mutation_protected_paths
        )

    def _reject_protected_mutation(self, path: Path) -> None:
        if self._is_mutation_protected_path(path):
            raise WorkspaceError(
                "Das Ändern von Secret-/Credential- oder geschützten "
                "Workspace-Dateien ist über den Workspace-Dateizugriff nicht erlaubt."
            )

    @staticmethod
    def _reject_hardlinked_file(path: Path) -> None:
        try:
            hardlinked = regular_file_has_multiple_links(path)
        except OSError as exc:
            raise WorkspaceError(
                f"Dateimetadaten konnten nicht sicher geprüft werden: {path.name!r}"
            ) from exc
        if hardlinked:
            raise WorkspaceError(
                "Dateien mit mehreren Hardlinks werden vom Workspace-Dateizugriff "
                "aus Sicherheitsgründen nicht verarbeitet."
            )

    @staticmethod
    def _limit(value: int, *, name: str, maximum: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
            raise WorkspaceError(f"{name} muss zwischen 1 und {maximum} liegen.")
        return value

    @staticmethod
    def _existing_line_ending(path: Path) -> str:
        """Return the dominant line ending, defaulting to LF on ties."""
        if not path.exists():
            return "\n"

        data = path.read_bytes()
        crlf_count = data.count(b"\r\n")
        lf_count = data.count(b"\n") - crlf_count
        cr_count = data.count(b"\r") - crlf_count

        if crlf_count > lf_count and crlf_count > cr_count:
            return "\r\n"
        if cr_count > lf_count and cr_count > crlf_count:
            return "\r"
        return "\n"

