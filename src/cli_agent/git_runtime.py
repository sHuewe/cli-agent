from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import threading
from itertools import islice
from pathlib import Path

from .filesystem_security import path_entry_is_symlink_or_reparse
from .git_operations import (
    GIT_TIMEOUT_SECONDS,
    MAX_BLAME_LINES,
    MAX_GREP_TEXT_LENGTH,
    MAX_OUTPUT,
    GitRepository,
    GitWorkspace,
    GitWorkspaceError,
)

MAX_COLLECTED_RECORDS = 10_000


class RuntimeGitWorkspace(GitWorkspace):
    """Git workspace with an operation-oriented trust boundary."""

    @classmethod
    def _git(cls, directory: Path, *args: str, allowed_returncodes: tuple[int, ...] = (0,)) -> str:
        """Run Git with a hard in-memory output bound while the process runs."""
        try:
            with tempfile.TemporaryFile() as errors, subprocess.Popen(
                cls._git_command(directory, *args),
                stdout=subprocess.PIPE,
                stderr=errors,
                env=cls._environment(),
            ) as process:
                timed_out = threading.Event()

                def expire() -> None:
                    timed_out.set()
                    if process.poll() is None:
                        process.kill()

                timer = threading.Timer(GIT_TIMEOUT_SECONDS, expire)
                timer.daemon = True
                timer.start()
                output = bytearray()
                try:
                    assert process.stdout is not None
                    while True:
                        chunk = process.stdout.read1(65_536)
                        if timed_out.is_set():
                            raise GitWorkspaceError("Git-Aufruf hat das Zeitlimit überschritten.")
                        if not chunk:
                            break
                        output.extend(chunk)
                        if len(output) > MAX_OUTPUT:
                            process.kill()
                            raise GitWorkspaceError("Git-Ausgabe ist zu groß; Abfrage weiter eingrenzen.")
                    process.wait()
                    if timed_out.is_set():
                        raise GitWorkspaceError("Git-Aufruf hat das Zeitlimit überschritten.")
                    if process.returncode not in allowed_returncodes:
                        errors.seek(0)
                        detail = errors.read(2000).decode("utf-8", errors="replace").strip()
                        if not detail and output:
                            detail = bytes(output[:2000]).decode("utf-8", errors="replace").strip()
                        raise GitWorkspaceError(f"Git-Aufruf fehlgeschlagen: {detail or f'Exit-Code {process.returncode}'}")
                    return bytes(output).decode("utf-8", errors="replace")
                finally:
                    timer.cancel()
                    if process.poll() is None:
                        process.kill()
                    process.wait()
                    timer.join()
        except FileNotFoundError as exc:
            raise GitWorkspaceError("Git ist nicht installiert oder nicht über PATH erreichbar.") from exc

    @classmethod
    def _probe(cls, workspace: Path, candidate: Path) -> GitRepository:
        try:
            root = candidate.resolve(strict=True)
            cls._inside(workspace, root, "Repository-Root")
            if root != candidate.absolute():
                raise GitWorkspaceError("Repository-Pfade dürfen nicht über Symlinks oder Reparse-Points umgeleitet werden.")
            # git blame reads the worktree mailmap automatically. Validate this
            # one special worktree metadata file without scanning the worktree.
            if os.path.lexists(candidate / ".mailmap"):
                cls._metadata_text(workspace, candidate / ".mailmap")
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
                if stat.S_ISREG(status.st_mode) and status.st_nlink > 1:
                    raise GitWorkspaceError("Git-Steuerdateien mit mehreren Hardlinks sind nicht erlaubt.")
                cls._inside(workspace, path.resolve(strict=True), "Git-Steuerpfad")
            except GitWorkspaceError:
                raise
            except OSError as exc:
                raise GitWorkspaceError("Git-Steuerpfad konnte nicht sicher geprüft werden.") from exc

    @classmethod
    def _reject_alternate_object_stores(cls, common_dir: Path) -> None:
        info_dir = common_dir / "objects" / "info"
        for name in ("alternates", "http-alternates"):
            if os.path.lexists(info_dir / name):
                raise GitWorkspaceError("Git-Repositories mit alternativen Object Stores werden nicht unterstützt.")

    @staticmethod
    def _bounded_records(records, *, label: str) -> list[str]:
        values: list[str] = []
        total = 0
        for value in records:
            total += len(value.encode("utf-8")) + 1
            if total > MAX_OUTPUT or len(values) >= MAX_COLLECTED_RECORDS:
                raise GitWorkspaceError(f"{label} ist zu groß; Abfrage weiter eingrenzen.")
            values.append(value)
        return values

    @staticmethod
    def _history_records(output: str) -> list[dict[str, str]]:
        fields = output.split("\x00")
        if fields and fields[-1] == "":
            fields.pop()
        records: list[dict[str, str]] = []
        if len(fields) % 5:
            raise GitWorkspaceError("Git-Historie konnte nicht ausgewertet werden.")
        for offset in range(0, len(fields), 5):
            values = fields[offset : offset + 5]
            # Pretty-format inserts a line break between commits. It is framing,
            # not part of the next object id.
            values[0] = values[0].lstrip("\r\n")
            if not values[0]:
                raise GitWorkspaceError("Git-Historie konnte nicht ausgewertet werden.")
            records.append(dict(zip(("id", "author", "email", "date", "message"), values, strict=True)))
        return records

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
            return self._bounded_records(records, label="Liste geänderter Dateien")

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

    def git_log(self, repository: str, max_count: int = 20) -> str:
        repo = self._repo(repository)
        output = self._git(repo.root, "log", f"--max-count={self._count(max_count)}", "--format=%H%x00%an%x00%ae%x00%aI%x00%s%x00")
        return json.dumps(self._history_records(output), ensure_ascii=False, indent=2)

    def git_commit_info(self, repository: str, commit_hash: str) -> str:
        repo = self._repo(repository)
        commit = self._commit(repo, commit_hash)
        records = self._history_records(self._git(repo.root, "show", "-s", "--format=%H%x00%an%x00%ae%x00%aI%x00%s%x00", commit))
        if len(records) != 1:
            raise GitWorkspaceError("Commit-Metadaten konnten nicht eindeutig ausgewertet werden.")
        return json.dumps(records[0], ensure_ascii=False, indent=2)

    def git_file_history(self, repository: str, path: str, max_count: int = 20) -> str:
        repo = self._repo(repository)
        output = self._git(repo.root, "log", "--follow", f"--max-count={self._count(max_count)}", "--format=%H%x00%an%x00%ae%x00%aI%x00%s%x00", "--", self._repo_path(repo, path))
        result = [{"author": {"name": item["author"], "email": item["email"]}, "date": item["date"], "commit": {"id": item["id"], "message": item["message"]}} for item in self._history_records(output)]
        return json.dumps(result, ensure_ascii=False, indent=2)

    def git_commit_files(self, repository: str, commit_hash: str) -> str:
        repo = self._repo(repository)
        commit = self._commit(repo, commit_hash)
        parents = self._commit_parents(repo, commit)
        if len(parents) > 1:
            args = ["diff", "--name-status", "-z", "-M", parents[0], commit]
        else:
            args = ["diff-tree", "--root", "--no-commit-id", "--name-status", "-z", "-r", "-M", commit]
        with self._git_null_records(repo.root, *args) as records:
            fields = self._bounded_records(records, label="Commit-Dateiliste")
        result = json.dumps(self._name_status_records("\x00".join(fields) + "\x00"), ensure_ascii=False, indent=2)
        if len(result.encode("utf-8")) > MAX_OUTPUT:
            raise GitWorkspaceError("Commit-Dateiliste ist zu groß; Abfrage weiter eingrenzen.")
        return result

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
