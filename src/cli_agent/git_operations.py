from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePath, PureWindowsPath

COMMIT_RE = re.compile(r"^[0-9a-fA-F]{4,64}$")
MAX_OUTPUT = 500_000


class GitWorkspaceError(RuntimeError):
    pass


@dataclass(frozen=True)
class GitRepository:
    root: Path
    git_dir: Path
    common_dir: Path
    relative_path: str


@dataclass(frozen=True)
class GitWorkspace:
    directory: Path
    repositories: tuple[GitRepository, ...]

    @classmethod
    def from_directory(cls, directory: Path) -> "GitWorkspace":
        workspace = directory.expanduser().resolve()
        if not workspace.is_dir():
            raise GitWorkspaceError(f"Projekt-Workspace existiert nicht: {workspace}")
        return cls(workspace, cls._discover(workspace))

    @classmethod
    def _discover(cls, workspace: Path) -> tuple[GitRepository, ...]:
        candidates: list[Path] = []
        for current, dirs, files in os.walk(workspace, topdown=True, followlinks=False):
            base = Path(current)
            if ".git" in dirs or ".git" in files:
                candidates.append(base)
            dirs[:] = [name for name in dirs if name != ".git" and not (base / name).is_symlink()]
        found: dict[Path, GitRepository] = {}
        for candidate in candidates:
            try:
                repo = cls._probe(workspace, candidate)
            except GitWorkspaceError:
                continue
            found[repo.root] = repo
        return tuple(sorted(found.values(), key=lambda item: item.relative_path.casefold()))

    @classmethod
    def _probe(cls, workspace: Path, candidate: Path) -> GitRepository:
        root = Path(cls._git(candidate, "rev-parse", "--show-toplevel").strip()).resolve(strict=True)
        git_dir = Path(cls._git(candidate, "rev-parse", "--absolute-git-dir").strip()).resolve(strict=True)
        common_raw = Path(cls._git(candidate, "rev-parse", "--git-common-dir").strip())
        common_dir = common_raw.resolve(strict=True) if common_raw.is_absolute() else (candidate / common_raw).resolve(strict=True)
        if root != candidate.resolve(strict=True):
            raise GitWorkspaceError("Repository-Root stimmt nicht mit dem gefundenen .git-Eintrag überein.")
        for label, path in (("Repository-Root", root), ("Git-Verzeichnis", git_dir), ("Git-Common-Verzeichnis", common_dir)):
            cls._inside(workspace, path, label)
        return GitRepository(root, git_dir, common_dir, root.relative_to(workspace).as_posix() or ".")

    @staticmethod
    def _inside(workspace: Path, path: Path, label: str) -> None:
        try:
            path.relative_to(workspace)
        except ValueError as exc:
            raise GitWorkspaceError(f"{label} liegt außerhalb des Projekt-Workspaces.") from exc

    @staticmethod
    def _environment() -> dict[str, str]:
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat", "PAGER": "cat"})
        return env

    @classmethod
    def _git(cls, directory: Path, *args: str) -> str:
        try:
            result = subprocess.run(
                ["git", "-C", str(directory), "--no-pager", "-c", "color.ui=false", "-c", "core.quotepath=false", *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                env=cls._environment(),
                check=False,
            )
        except FileNotFoundError as exc:
            raise GitWorkspaceError("Git ist nicht installiert oder nicht über PATH erreichbar.") from exc
        except subprocess.TimeoutExpired as exc:
            raise GitWorkspaceError("Git-Aufruf hat das Zeitlimit überschritten.") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or f"Exit-Code {result.returncode}").strip()
            raise GitWorkspaceError(f"Git-Aufruf fehlgeschlagen: {detail[:2000]}")
        if len(result.stdout) > MAX_OUTPUT:
            raise GitWorkspaceError("Git-Ausgabe ist zu groß; Abfrage weiter eingrenzen.")
        return result.stdout

    def _repo(self, repository: str) -> GitRepository:
        normalized = self._relative(repository, allow_dot=True).as_posix()
        normalized = "." if normalized in {"", "."} else normalized
        for repo in self.repositories:
            if repo.relative_path == normalized:
                return repo
        raise GitWorkspaceError(f"Unbekanntes oder nicht freigegebenes Git-Repository: {repository!r}")

    @staticmethod
    def _relative(path: str, *, allow_dot: bool = False) -> PurePath:
        if not isinstance(path, str) or not path.strip():
            raise GitWorkspaceError("Der Pfad darf nicht leer sein.")
        raw = path.strip()
        native, windows = Path(raw), PureWindowsPath(raw)
        pure = PurePath(raw)
        if native.is_absolute() or windows.is_absolute() or windows.drive:
            raise GitWorkspaceError("Der Pfad muss relativ sein.")
        if ".." in pure.parts or ".." in windows.parts:
            raise GitWorkspaceError("Der Pfad darf '..' nicht enthalten.")
        if not allow_dot and pure.as_posix() in {"", "."}:
            raise GitWorkspaceError("Dateipfad darf nicht der Repository-Root sein.")
        return pure

    def _repo_path(self, repo: GitRepository, path: str) -> str:
        pure = self._relative(path)
        resolved = (repo.root / Path(pure.as_posix())).resolve(strict=False)
        try:
            resolved.relative_to(repo.root)
            resolved.relative_to(self.directory)
        except ValueError as exc:
            raise GitWorkspaceError("Pfad verweist außerhalb des Repositorys oder Workspaces.") from exc
        return pure.as_posix()

    @staticmethod
    def _count(value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 200:
            raise GitWorkspaceError("max_count muss zwischen 1 und 200 liegen.")
        return value

    @classmethod
    def _commit(cls, repo: GitRepository, value: str) -> str:
        if not isinstance(value, str) or not COMMIT_RE.fullmatch(value.strip()):
            raise GitWorkspaceError("commit_hash muss ein hexadezimaler Git-Commit-Hash sein.")
        return cls._git(repo.root, "rev-parse", "--verify", f"{value.strip()}^{{commit}}").strip()

    @staticmethod
    def _records(output: str) -> list[dict[str, str]]:
        records = []
        for record in output.split("\x1e"):
            record = record.strip("\r\n")
            if not record:
                continue
            values = record.split("\x00")
            if len(values) != 5:
                raise GitWorkspaceError("Git-Historie konnte nicht ausgewertet werden.")
            records.append(dict(zip(("id", "author", "email", "date", "message"), values, strict=True)))
        return records

    def git_repositories(self) -> str:
        return json.dumps([{"path": repo.relative_path} for repo in self.repositories], ensure_ascii=False, indent=2)

    def git_status(self, repository: str) -> str:
        output = self._git(self._repo(repository).root, "status", "--short", "--branch", "--untracked-files=all").strip()
        return output or "(working tree clean)"

    def git_current_branch(self, repository: str) -> str:
        repo = self._repo(repository)
        result = subprocess.run(["git", "-C", str(repo.root), "symbolic-ref", "--quiet", "--short", "HEAD"], capture_output=True, text=True, env=self._environment(), check=False)
        return result.stdout.strip() if result.returncode == 0 else f"(detached HEAD at {self._git(repo.root, 'rev-parse', '--short', 'HEAD').strip()})"

    def git_branches(self, repository: str) -> str:
        output = self._git(self._repo(repository).root, "branch", "--format=%(refname:short)").strip()
        return output or "(no local branches)"

    def git_diff(self, repository: str, path: str | None = None) -> str:
        repo = self._repo(repository)
        args = ["diff", "--no-ext-diff", "--no-textconv"]
        if path is not None:
            args += ["--", self._repo_path(repo, path)]
        return self._git(repo.root, *args).strip() or "(no unstaged changes)"

    def git_diff_staged(self, repository: str, path: str | None = None) -> str:
        repo = self._repo(repository)
        args = ["diff", "--cached", "--no-ext-diff", "--no-textconv"]
        if path is not None:
            args += ["--", self._repo_path(repo, path)]
        return self._git(repo.root, *args).strip() or "(no staged changes)"

    def git_log(self, repository: str, max_count: int = 20) -> str:
        repo = self._repo(repository)
        output = self._git(repo.root, "log", f"--max-count={self._count(max_count)}", "--format=%H%x00%an%x00%ae%x00%aI%x00%s%x1e")
        return json.dumps(self._records(output), ensure_ascii=False, indent=2)

    def git_commit_info(self, repository: str, commit_hash: str) -> str:
        repo = self._repo(repository)
        commit = self._commit(repo, commit_hash)
        records = self._records(self._git(repo.root, "show", "-s", "--format=%H%x00%an%x00%ae%x00%aI%x00%s%x1e", commit))
        if len(records) != 1:
            raise GitWorkspaceError("Commit-Metadaten konnten nicht eindeutig ausgewertet werden.")
        return json.dumps(records[0], ensure_ascii=False, indent=2)

    def git_commit_diff(self, repository: str, commit_hash: str, path: str | None = None) -> str:
        repo = self._repo(repository)
        args = ["show", "--format=", "--patch", "--no-ext-diff", "--no-textconv", self._commit(repo, commit_hash)]
        if path is not None:
            args += ["--", self._repo_path(repo, path)]
        return self._git(repo.root, *args).strip() or "(commit has no textual diff for this selection)"

    def git_file_history(self, repository: str, path: str, max_count: int = 20) -> str:
        repo = self._repo(repository)
        output = self._git(repo.root, "log", "--follow", f"--max-count={self._count(max_count)}", "--format=%H%x00%an%x00%ae%x00%aI%x00%s%x1e", "--", self._repo_path(repo, path))
        records = self._records(output)
        result = [{"author": {"name": item["author"], "email": item["email"]}, "date": item["date"], "commit": {"id": item["id"], "message": item["message"]}} for item in records]
        return json.dumps(result, ensure_ascii=False, indent=2)

    def git_commit_files(self, repository: str, commit_hash: str) -> str:
        repo = self._repo(repository)
        output = self._git(repo.root, "diff-tree", "--root", "--no-commit-id", "--name-status", "-r", "-M", self._commit(repo, commit_hash))
        files: list[dict[str, str]] = []
        labels = {"A": "added", "C": "copied", "D": "deleted", "M": "modified", "R": "renamed", "T": "type_changed"}
        for line in output.splitlines():
            parts = line.split("\t")
            kind = parts[0][:1]
            if kind in {"R", "C"} and len(parts) == 3:
                files.append({"status": labels.get(kind, "unknown"), "old_path": parts[1], "path": parts[2]})
            elif len(parts) == 2:
                files.append({"status": labels.get(kind, "unknown"), "path": parts[1]})
            elif line:
                raise GitWorkspaceError("Commit-Dateiliste konnte nicht ausgewertet werden.")
        return json.dumps(files, ensure_ascii=False, indent=2)
