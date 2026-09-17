from __future__ import annotations

import os
import stat
from pathlib import Path

from .filesystem_security import path_entry_is_symlink_or_reparse
from .git_operations import GitRepository, GitWorkspace, GitWorkspaceError


class RuntimeGitWorkspace(GitWorkspace):
    """Git workspace with a deliberately narrow, operation-oriented trust boundary.

    Git's local object database (including configured alternates) is treated as
    repository data. We do not recursively audit it. Security checks instead
    protect the model-controlled workspace boundary, reject filesystem
    indirection in repository control paths, prevent repository-controlled
    process/network capabilities, and validate working-tree files before tools
    return their contents.
    """

    @classmethod
    def _probe(cls, workspace: Path, candidate: Path) -> GitRepository:
        """Discover a repository without recursively walking its object store."""
        try:
            root = candidate.resolve(strict=True)
            cls._inside(workspace, root, "Repository-Root")
            if root != candidate.absolute():
                raise GitWorkspaceError(
                    "Repository-Pfade dürfen nicht über Symlinks oder Reparse-Points umgeleitet werden."
                )

            marker = candidate / ".git"
            if marker.is_dir():
                if path_entry_is_symlink_or_reparse(marker):
                    raise GitWorkspaceError(
                        "Git-Verzeichnisse dürfen keine Symlinks oder Reparse-Points sein."
                    )
                expected_git_dir = marker.resolve(strict=True)
            else:
                gitfile = cls._metadata_text(workspace, marker).rstrip("\r\n")
                if not gitfile.startswith("gitdir: "):
                    raise GitWorkspaceError("Ungültiger Git-Verzeichniseintrag.")
                expected_git_dir = (candidate / gitfile[8:]).resolve(strict=True)
            cls._inside(workspace, expected_git_dir, "Git-Verzeichnis")

            common_file = expected_git_dir / "commondir"
            expected_common_dir = expected_git_dir
            if os.path.lexists(common_file):
                common_value = cls._metadata_text(workspace, common_file).rstrip("\r\n")
                if not common_value:
                    raise GitWorkspaceError("Leeres Git-Common-Verzeichnis.")
                expected_common_dir = (expected_git_dir / common_value).resolve(strict=True)
            cls._inside(workspace, expected_common_dir, "Git-Common-Verzeichnis")

            cls._validate_control_paths(workspace, expected_git_dir, expected_common_dir)
            cls._validate_repository_config(
                workspace, candidate, expected_git_dir, expected_common_dir
            )

            git_root = Path(
                cls._git(candidate, "rev-parse", "--show-toplevel").removesuffix("\n")
            ).resolve(strict=True)
            git_dir = Path(
                cls._git(candidate, "rev-parse", "--absolute-git-dir").removesuffix("\n")
            ).resolve(strict=True)
            common_raw = Path(
                cls._git(candidate, "rev-parse", "--git-common-dir").removesuffix("\n")
            )
            common_dir = (
                common_raw.resolve(strict=True)
                if common_raw.is_absolute()
                else (candidate / common_raw).resolve(strict=True)
            )
        except GitWorkspaceError:
            raise
        except (OSError, ValueError, RuntimeError) as exc:
            raise GitWorkspaceError(
                "Git-Repositorypfade konnten nicht sicher aufgelöst werden."
            ) from exc

        if git_root != root:
            raise GitWorkspaceError(
                "Repository-Root stimmt nicht mit dem gefundenen .git-Eintrag überein."
            )
        for label, path in (
            ("Repository-Root", git_root),
            ("Git-Verzeichnis", git_dir),
            ("Git-Common-Verzeichnis", common_dir),
        ):
            cls._inside(workspace, path, label)
        if git_dir != expected_git_dir or common_dir != expected_common_dir:
            raise GitWorkspaceError(
                "Git-Repositorypfade haben sich während der Prüfung geändert."
            )

        return GitRepository(
            git_root,
            git_dir,
            common_dir,
            git_root.relative_to(workspace).as_posix() or ".",
        )

    @classmethod
    def _validate_control_paths(
        cls, workspace: Path, git_dir: Path, common_dir: Path
    ) -> None:
        """Reject indirection in small Git control paths, not object contents."""
        paths = {
            git_dir / "HEAD",
            git_dir / "index",
            git_dir / "commondir",
            git_dir / "config",
            git_dir / "config.worktree",
            common_dir / "HEAD",
            common_dir / "packed-refs",
            common_dir / "config",
            common_dir / "refs",
            common_dir / "objects",
        }
        for path in paths:
            if not os.path.lexists(path):
                continue
            try:
                if path_entry_is_symlink_or_reparse(path):
                    raise GitWorkspaceError(
                        "Symlinks oder Reparse-Points in Git-Steuerpfaden sind nicht erlaubt."
                    )
                status = os.lstat(path)
                if not (stat.S_ISREG(status.st_mode) or stat.S_ISDIR(status.st_mode)):
                    raise GitWorkspaceError(
                        "Git-Steuerpfade müssen reguläre Dateien oder Verzeichnisse sein."
                    )
                cls._inside(workspace, path.resolve(strict=True), "Git-Steuerpfad")
            except GitWorkspaceError:
                raise
            except OSError as exc:
                raise GitWorkspaceError(
                    "Git-Steuerpfad konnte nicht sicher geprüft werden."
                ) from exc

    def _repo(self, repository: str) -> GitRepository:
        normalized = self._relative(repository, allow_dot=True).as_posix()
        normalized = "." if normalized in {"", "."} else normalized
        for repo in self.repositories:
            if repo.relative_path != normalized:
                continue
            candidate = self.directory if normalized == "." else self.directory / normalized
            # Re-probe the small set of trust-boundary/control paths. This
            # catches post-discovery repository redirects without walking every
            # object/ref file on every tool call.
            refreshed = self._probe(self.directory, candidate)
            if refreshed.relative_path != normalized:
                raise GitWorkspaceError(
                    "Repository-Pfade dürfen nicht über Symlinks oder Reparse-Points umgeleitet werden."
                )
            return refreshed
        raise GitWorkspaceError(
            f"Unbekanntes oder nicht freigegebenes Git-Repository: {repository!r}"
        )

    def git_status(self, repository: str) -> str:
        # Status returns names/state, not working-tree file contents. Avoid the
        # O(number-of-tracked-files) content-read validation used by grep/diff/blame.
        repo = self._repo(repository)
        output = self._git(
            repo.root,
            "status",
            "--short",
            "--branch",
            "--untracked-files=all",
            "--ignore-submodules=all",
        ).strip()
        return output or "(working tree clean)"
