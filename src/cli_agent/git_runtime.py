from __future__ import annotations

import json
import os
import stat
from itertools import islice
from pathlib import Path

from .filesystem_security import path_entry_is_symlink_or_reparse
from .git_operations import (
    MAX_BLAME_LINES,
    MAX_GREP_TEXT_LENGTH,
    MAX_OUTPUT,
    GitRepository,
    GitWorkspace,
    GitWorkspaceError,
)


class RuntimeGitWorkspace(GitWorkspace):
    """Git workspace with an operation-oriented trust boundary.

    Git's local object database is repository data and is not recursively
    audited. Alternate object stores are deliberately unsupported. Working-tree
    paths are validated lazily: only files whose contents are about to cross
    the MCP boundary are inspected. A whole-repository working-tree validation
    is deliberately unsupported because it is not acceptably bounded for real
    repositories.
    """

    @classmethod
    def _probe(cls, workspace: Path, candidate: Path) -> GitRepository:
        try:
            root = candidate.resolve(strict=True)
            cls._inside(workspace, root, "Repository-Root")
            if root != candidate.absolute():
                raise GitWorkspaceError("Repository-Pfade dürfen nicht über Symlinks oder Reparse-Points umgeleitet werden.")
            marker = candidate / ".git"
            if marker.is_dir():
                if path_entry_is_symlink_or_reparse(marker):
                    raise GitWorkspaceError("Git-Verzeichnisse dürfen keine Symlinks oder Reparse-Points sein.")
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
            cls._reject_alternate_object_stores(expected_common_dir)
            cls._validate_repository_config(workspace, candidate, expected_git_dir, expected_common_dir)
            git_root = Path(cls._git(candidate, "rev-parse", "--show-toplevel").removesuffix("\n")).resolve(strict=True)
            git_dir = Path(cls._git(candidate, "rev-parse", "--absolute-git-dir").removesuffix("\n")).resolve(strict=True)
            common_raw = Path(cls._git(candidate, "rev-parse", "--git-common-dir").removesuffix("\n"))
            common_dir = common_raw.resolve(strict=True) if common_raw.is_absolute() else (candidate / common_raw).resolve(strict=True)
        except GitWorkspaceError:
            raise
        except (OSError, ValueError, RuntimeError) as exc:
            raise GitWorkspaceError("Git-Repositorypfade konnten nicht sicher aufgelöst werden.") from exc
        if git_root != root:
            raise GitWorkspaceError("Repository-Root stimmt nicht mit dem gefundenen .git-Eintrag überein.")
        for label, path in (("Repository-Root", git_root), ("Git-Verzeichnis", git_dir), ("Git-Common-Verzeichnis", common_dir)):
            cls._inside(workspace, path, label)
        if git_dir != expected_git_dir or common_dir != expected_common_dir:
            raise GitWorkspaceError("Git-Repositorypfade haben sich während der Prüfung geändert.")
        return GitRepository(git_root, git_dir, common_dir, git_root.relative_to(workspace).as_posix() or ".")

    @classmethod
    def _validate_control_paths(cls, workspace: Path, git_dir: Path, common_dir: Path) -> None:
        paths = {git_dir / "HEAD", git_dir / "index", git_dir / "commondir", git_dir / "config", git_dir / "config.worktree", common_dir / "HEAD", common_dir / "packed-refs", common_dir / "config", common_dir / "refs", common_dir / "objects"}
        for path in paths:
            if not os.path.lexists(path):
                continue
            try:
                if path_entry_is_symlink_or_reparse(path):
                    raise GitWorkspaceError("Symlinks oder Reparse-Points in Git-Steuerpfaden sind nicht erlaubt.")
                status = os.lstat(path)
                if not (stat.S_ISREG(status.st_mode) or stat.S_ISDIR(status.st_mode)):
                    raise GitWorkspaceError("Git-Steuerpfade müssen reguläre Dateien oder Verzeichnisse sein.")
                cls._inside(workspace, path.resolve(strict=True), "Git-Steuerpfad")
            except GitWorkspaceError:
                raise
            except OSError as exc:
                raise GitWorkspaceError("Git-Steuerpfad konnte nicht sicher geprüft werden.") from exc

    @classmethod
    def _reject_alternate_object_stores(cls, common_dir: Path) -> None:
        """Reject repositories that can source objects from alternate stores.

        This is intentionally a constant-size control-file check. We do not
        traverse the object database or an alternate graph: repositories using
        alternates are simply outside the supported Git MCP subset.
        """
        info_dir = common_dir / "objects" / "info"
        for name in ("alternates", "http-alternates"):
            if os.path.lexists(info_dir / name):
                raise GitWorkspaceError("Git-Repositories mit alternativen Object Stores werden nicht unterstützt.")

    def _repo(self, repository: str) -> GitRepository:
        normalized = self._relative(repository, allow_dot=True).as_posix()
        normalized = "." if normalized in {"", "."} else normalized
        for repo in self.repositories:
            if repo.relative_path != normalized:
                continue
            candidate = self.directory if normalized == "." else self.directory / normalized
            refreshed = self._probe(self.directory, candidate)
            if refreshed.relative_path != normalized:
                raise GitWorkspaceError("Repository-Pfade dürfen nicht über Symlinks oder Reparse-Points umgeleitet werden.")
            return refreshed
        raise GitWorkspaceError(f"Unbekanntes oder nicht freigegebenes Git-Repository: {repository!r}")

    def _validate_worktree_files(self, repo: GitRepository, path: str | None = None) -> None:
        raise GitWorkspaceError("Eine vollständige Working-Tree-Prüfung wird nicht unterstützt; Tools müssen relevante Dateien einzeln validieren.")

    def _validate_content_path(self, repo: GitRepository, relative: str) -> None:
        pure = self._relative(relative)
        entry = repo.root / Path(pure.as_posix())
        try:
            current = repo.root
            for component in pure.parts:
                current /= component
                if path_entry_is_symlink_or_reparse(current):
                    raise GitWorkspaceError("Symlinks oder Reparse-Points in Working-Tree-Pfaden sind nicht erlaubt.")
            status = os.lstat(entry)
            if not stat.S_ISREG(status.st_mode):
                raise GitWorkspaceError("Working-Tree-Inhalte müssen aus regulären Dateien stammen.")
            if status.st_nlink > 1:
                raise GitWorkspaceError("Working-Tree-Dateien mit mehreren Hardlinks werden aus Sicherheitsgründen nicht gelesen.")
            resolved = entry.resolve(strict=True)
            resolved.relative_to(repo.root)
            resolved.relative_to(self.directory)
        except GitWorkspaceError:
            raise
        except (FileNotFoundError, OSError, ValueError) as exc:
            raise GitWorkspaceError("Working-Tree-Datei konnte nicht sicher aufgelöst werden.") from exc

    def _changed_worktree_paths(self, repo: GitRepository, path: str | None) -> list[str]:
        args = ["diff", "--name-only", "-z", "--no-ext-diff", "--no-textconv", "--ignore-submodules=all"]
        if path is not None:
            args += ["--", path]
        with self._git_null_records(repo.root, *args) as records:
            return list(records)

    def git_status(self, repository: str) -> str:
        repo = self._repo(repository)
        output = self._git(repo.root, "status", "--short", "--branch", "--untracked-files=all", "--ignore-submodules=all").strip()
        return output or "(working tree clean)"

    def git_diff(self, repository: str, path: str | None = None) -> str:
        repo = self._repo(repository)
        repo_path = self._repo_path(repo, path) if path is not None else None
        for filename in self._changed_worktree_paths(repo, repo_path):
            if os.path.lexists(repo.root / filename):
                self._validate_content_path(repo, filename)
        args = ["diff", "--no-ext-diff", "--no-textconv", "--ignore-submodules=all"]
        if repo_path is not None:
            args += ["--", repo_path]
        return self._git(repo.root, *args).strip() or "(no unstaged changes)"

    def git_commit_files(self, repository: str, commit_hash: str) -> str:
        repo = self._repo(repository)
        commit = self._commit(repo, commit_hash)
        parents = self._commit_parents(repo, commit)
        if len(parents) > 1:
            args = ["diff", "--name-status", "-z", "-M", parents[0], commit]
        else:
            args = ["diff-tree", "--root", "--no-commit-id", "--name-status", "-z", "-r", "-M", commit]
        with self._git_null_records(repo.root, *args) as records:
            fields = list(records)
        # _git_null_records decodes every filename-bearing field strictly, so
        # invalid UTF-8 is rejected instead of being rewritten with U+FFFD.
        return json.dumps(self._name_status_records("\x00".join(fields) + "\x00"), ensure_ascii=False, indent=2)

    def git_grep(self, repository: str, text: str, path: str | None = None, max_results: int = 50) -> str:
        if not isinstance(text, str) or not text:
            raise GitWorkspaceError("text darf nicht leer sein.")
        if len(text) > MAX_GREP_TEXT_LENGTH:
            raise GitWorkspaceError(f"text darf höchstens {MAX_GREP_TEXT_LENGTH} Zeichen lang sein.")
        limit = self._grep_limit(max_results)
        repo = self._repo(repository)
        repo_path = self._repo_path(repo, path) if path is not None else None
        file_args = ["grep", "-l", "-z", "-I", "-F", "-e", text]
        if repo_path is not None:
            file_args += ["--", repo_path]
        with self._git_null_records(repo.root, *file_args, allowed_returncodes=(0, 1)) as filenames:
            matching_files = list(islice(filenames, limit + 1))
        matches: list[dict[str, object]] = []
        result_size = 0
        truncated = False
        for file_index, filename in enumerate(matching_files):
            remaining = limit - len(matches)
            if remaining <= 0:
                truncated = True
                break
            self._validate_content_path(repo, filename)
            output = self._git(repo.root, "grep", "-n", "-z", "-I", "-F", f"--max-count={remaining + 1}", "-e", text, "--", filename, allowed_returncodes=(0, 1))
            file_matches = self._grep_records(output)
            selected = file_matches[:remaining]
            for match in selected:
                result_size += len(str(match["path"])) + len(str(match["text"])) + 64
                if result_size > MAX_OUTPUT:
                    raise GitWorkspaceError("Git-Grep-Ergebnis ist zu groß; Suche weiter eingrenzen.")
                matches.append(match)
            if len(file_matches) > remaining or (len(matches) >= limit and file_index < len(matching_files) - 1):
                truncated = True
                break
        return json.dumps({"matches": matches, "truncated": truncated}, ensure_ascii=False, indent=2)

    def git_blame(self, repository: str, path: str, start_line: int | None = None, end_line: int | None = None) -> str:
        repo = self._repo(repository)
        repo_path = self._repo_path(repo, path)
        self._validate_content_path(repo, repo_path)
        line_range = self._blame_range(start_line, end_line)
        args = ["blame", "--line-porcelain", "--no-textconv"]
        if line_range is None:
            status = (repo.root / repo_path).lstat()
            if not (stat.S_ISREG(status.st_mode) and status.st_size == 0):
                args += ["-L", f"1,{MAX_BLAME_LINES + 1}"]
        else:
            args += ["-L", f"{line_range[0]},{line_range[1]}"]
        args += ["--", repo_path]
        records = self._blame_records(self._git(repo.root, *args))
        if line_range is None and len(records) > MAX_BLAME_LINES:
            raise GitWorkspaceError(f"Datei hat mehr als {MAX_BLAME_LINES} Zeilen; start_line und end_line müssen angegeben werden.")
        return json.dumps(records, ensure_ascii=False, indent=2)
