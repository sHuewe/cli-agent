from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePath, PureWindowsPath

COMMIT_RE = re.compile(r"^[0-9a-fA-F]{4,64}$")
BLAME_TZ_RE = re.compile(r"^([+-])(\d{2})(\d{2})$")
MAX_OUTPUT = 500_000
MAX_GREP_RESULTS = 200
MAX_GREP_TEXT_LENGTH = 4_096
MAX_BLAME_LINES = 200


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
        try:
            root = Path(cls._git(candidate, "rev-parse", "--show-toplevel").strip()).resolve(strict=True)
            git_dir = Path(cls._git(candidate, "rev-parse", "--absolute-git-dir").strip()).resolve(strict=True)
            common_raw = Path(cls._git(candidate, "rev-parse", "--git-common-dir").strip())
            common_dir = common_raw.resolve(strict=True) if common_raw.is_absolute() else (candidate / common_raw).resolve(strict=True)
        except OSError as exc:
            raise GitWorkspaceError("Git-Repositorypfade konnten nicht sicher aufgelöst werden.") from exc
        if root != candidate.resolve(strict=True):
            raise GitWorkspaceError("Repository-Root stimmt nicht mit dem gefundenen .git-Eintrag überein.")
        for label, path in (("Repository-Root", root), ("Git-Verzeichnis", git_dir), ("Git-Common-Verzeichnis", common_dir)):
            cls._inside(workspace, path, label)

        objects_raw = Path(cls._git(candidate, "rev-parse", "--git-path", "objects").strip())
        try:
            objects_dir = objects_raw.resolve(strict=True) if objects_raw.is_absolute() else (candidate / objects_raw).resolve(strict=True)
        except OSError as exc:
            raise GitWorkspaceError("Git-Objektverzeichnis konnte nicht sicher aufgelöst werden.") from exc
        cls._inside(workspace, objects_dir, "Git-Objektverzeichnis")
        cls._validate_alternates(workspace, objects_dir)
        return GitRepository(root, git_dir, common_dir, root.relative_to(workspace).as_posix() or ".")

    @staticmethod
    def _inside(workspace: Path, path: Path, label: str) -> None:
        try:
            path.relative_to(workspace)
        except ValueError as exc:
            raise GitWorkspaceError(f"{label} liegt außerhalb des Projekt-Workspaces.") from exc

    @classmethod
    def _validate_alternates(cls, workspace: Path, objects_dir: Path) -> None:
        alternates = objects_dir / "info" / "alternates"
        if alternates.exists():
            try:
                values = alternates.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError) as exc:
                raise GitWorkspaceError("Git-Alternates konnten nicht sicher gelesen werden.") from exc
            for value in values:
                value = value.strip()
                if not value:
                    continue
                path = Path(value)
                try:
                    resolved = path.resolve(strict=True) if path.is_absolute() else (objects_dir / path).resolve(strict=True)
                except OSError as exc:
                    raise GitWorkspaceError("Git-Alternate-Objektverzeichnis ist ungültig oder nicht erreichbar.") from exc
                cls._inside(workspace, resolved, "Git-Alternate-Objektverzeichnis")
        http_alternates = objects_dir / "info" / "http-alternates"
        if http_alternates.exists():
            try:
                content = http_alternates.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeDecodeError) as exc:
                raise GitWorkspaceError("Git-HTTP-Alternates konnten nicht sicher gelesen werden.") from exc
            if content:
                raise GitWorkspaceError("Git-HTTP-Alternates sind nicht erlaubt.")

    @staticmethod
    def _environment() -> dict[str, str]:
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update(
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_NO_LAZY_FETCH": "1",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_PAGER": "cat",
                "PAGER": "cat",
            }
        )
        return env

    @classmethod
    def _git(
        cls,
        directory: Path,
        *args: str,
        allowed_returncodes: tuple[int, ...] = (0,),
    ) -> str:
        command = [
            "git", "-C", str(directory), "--no-pager",
            "-c", "color.ui=false",
            "-c", "core.quotepath=false",
            "-c", "core.fsmonitor=false",
            "-c", f"core.attributesFile={os.devnull}",
            "-c", f"core.excludesFile={os.devnull}",
            *args,
        ]
        try:
            result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20, env=cls._environment(), check=False)
        except FileNotFoundError as exc:
            raise GitWorkspaceError("Git ist nicht installiert oder nicht über PATH erreichbar.") from exc
        except subprocess.TimeoutExpired as exc:
            raise GitWorkspaceError("Git-Aufruf hat das Zeitlimit überschritten.") from exc
        if result.returncode not in allowed_returncodes:
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

    @staticmethod
    def _grep_limit(value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_GREP_RESULTS:
            raise GitWorkspaceError(f"max_results muss zwischen 1 und {MAX_GREP_RESULTS} liegen.")
        return value

    @staticmethod
    def _blame_range(start_line: int | None, end_line: int | None) -> tuple[int, int] | None:
        if start_line is None and end_line is None:
            return None
        if start_line is None or end_line is None:
            raise GitWorkspaceError("start_line und end_line müssen gemeinsam angegeben werden.")
        if isinstance(start_line, bool) or isinstance(end_line, bool) or not isinstance(start_line, int) or not isinstance(end_line, int):
            raise GitWorkspaceError("start_line und end_line müssen ganze Zahlen sein.")
        if start_line < 1 or end_line < start_line:
            raise GitWorkspaceError("Ungültiger Blame-Zeilenbereich.")
        if end_line - start_line + 1 > MAX_BLAME_LINES:
            raise GitWorkspaceError(f"git_blame darf höchstens {MAX_BLAME_LINES} Zeilen pro Aufruf analysieren.")
        return start_line, end_line

    @classmethod
    def _commit(cls, repo: GitRepository, value: str) -> str:
        if not isinstance(value, str) or not COMMIT_RE.fullmatch(value.strip()):
            raise GitWorkspaceError("commit_hash muss ein hexadezimaler Git-Commit-Hash sein.")
        return cls._git(repo.root, "rev-parse", "--verify", f"{value.strip()}^{{commit}}").strip()

    @classmethod
    def _commit_parents(cls, repo: GitRepository, commit: str) -> tuple[str, ...]:
        values = cls._git(repo.root, "rev-list", "--parents", "-n", "1", commit).strip().split()
        if not values or values[0] != commit:
            raise GitWorkspaceError("Commit-Eltern konnten nicht ausgewertet werden.")
        return tuple(values[1:])

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

    @staticmethod
    def _name_status_records(output: str) -> list[dict[str, str]]:
        fields = output.split("\x00")
        if fields and fields[-1] == "":
            fields.pop()
        files: list[dict[str, str]] = []
        labels = {"A": "added", "C": "copied", "D": "deleted", "M": "modified", "R": "renamed", "T": "type_changed"}
        index = 0
        while index < len(fields):
            status = fields[index]
            index += 1
            if not status:
                raise GitWorkspaceError("Commit-Dateiliste konnte nicht ausgewertet werden.")
            kind = status[:1]
            if kind in {"R", "C"}:
                if index + 1 >= len(fields):
                    raise GitWorkspaceError("Commit-Dateiliste konnte nicht ausgewertet werden.")
                old_path = fields[index]
                path = fields[index + 1]
                index += 2
                files.append({"status": labels.get(kind, "unknown"), "old_path": old_path, "path": path})
                continue
            if index >= len(fields):
                raise GitWorkspaceError("Commit-Dateiliste konnte nicht ausgewertet werden.")
            path = fields[index]
            index += 1
            files.append({"status": labels.get(kind, "unknown"), "path": path})
        return files

    @staticmethod
    def _grep_records(output: str) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []
        offset = 0
        while offset < len(output):
            path_end = output.find("\x00", offset)
            if path_end < 0:
                raise GitWorkspaceError("Git-Grep-Ausgabe konnte nicht ausgewertet werden.")
            line_end = output.find("\x00", path_end + 1)
            if line_end < 0:
                raise GitWorkspaceError("Git-Grep-Ausgabe konnte nicht ausgewertet werden.")
            text_end = output.find("\n", line_end + 1)
            if text_end < 0:
                text_end = len(output)
            line_text = output[path_end + 1:line_end]
            try:
                line_number = int(line_text)
            except ValueError as exc:
                raise GitWorkspaceError("Git-Grep-Zeilennummer konnte nicht ausgewertet werden.") from exc
            records.append(
                {
                    "path": output[offset:path_end],
                    "line": line_number,
                    "text": output[line_end + 1:text_end],
                }
            )
            offset = text_end + 1
        return records

    @staticmethod
    def _blame_date(timestamp: str, tz_value: str) -> str:
        try:
            timestamp_value = int(timestamp)
        except ValueError as exc:
            raise GitWorkspaceError("Git-Blame-Zeitstempel konnte nicht ausgewertet werden.") from exc
        match = BLAME_TZ_RE.fullmatch(tz_value)
        if match is None:
            raise GitWorkspaceError("Git-Blame-Zeitzone konnte nicht ausgewertet werden.")
        sign = 1 if match.group(1) == "+" else -1
        minutes = sign * (int(match.group(2)) * 60 + int(match.group(3)))
        tzinfo = datetime.timezone(datetime.timedelta(minutes=minutes))
        return datetime.datetime.fromtimestamp(timestamp_value, tz=datetime.UTC).astimezone(tzinfo).isoformat()

    @classmethod
    def _blame_records(cls, output: str) -> list[dict[str, object]]:
        lines = output.splitlines()
        records: list[dict[str, object]] = []
        index = 0
        while index < len(lines):
            header = lines[index].split()
            if len(header) < 3 or not COMMIT_RE.fullmatch(header[0]):
                raise GitWorkspaceError("Git-Blame-Ausgabe konnte nicht ausgewertet werden.")
            try:
                original_line = int(header[1])
                final_line = int(header[2])
            except ValueError as exc:
                raise GitWorkspaceError("Git-Blame-Zeilennummer konnte nicht ausgewertet werden.") from exc
            commit_hash = header[0]
            index += 1
            metadata: dict[str, str] = {}
            while index < len(lines) and not lines[index].startswith("\t"):
                key, separator, value = lines[index].partition(" ")
                metadata[key] = value if separator else ""
                index += 1
            if index >= len(lines):
                raise GitWorkspaceError("Git-Blame-Ausgabe enthält keinen Quelltext zur Zeile.")
            text = lines[index][1:]
            index += 1
            email = metadata.get("author-mail", "")
            if email.startswith("<") and email.endswith(">"):
                email = email[1:-1]
            if "author-time" not in metadata or "author-tz" not in metadata:
                raise GitWorkspaceError("Git-Blame-Ausgabe enthält keine vollständigen Autor-Zeitdaten.")
            records.append(
                {
                    "line": final_line,
                    "original_line": original_line,
                    "text": text,
                    "author": {
                        "name": metadata.get("author", ""),
                        "email": email,
                    },
                    "date": cls._blame_date(metadata["author-time"], metadata["author-tz"]),
                    "commit": {
                        "id": commit_hash,
                        "message": metadata.get("summary", ""),
                    },
                    "original_path": metadata.get("filename", ""),
                    "uncommitted": set(commit_hash) == {"0"},
                }
            )
        return records

    def git_repositories(self) -> str:
        return json.dumps([{"path": repo.relative_path} for repo in self.repositories], ensure_ascii=False, indent=2)

    def git_status(self, repository: str) -> str:
        output = self._git(self._repo(repository).root, "status", "--short", "--branch", "--untracked-files=all").strip()
        return output or "(working tree clean)"

    def git_current_branch(self, repository: str) -> str:
        repo = self._repo(repository)
        try:
            return self._git(repo.root, "symbolic-ref", "--quiet", "--short", "HEAD").strip()
        except GitWorkspaceError:
            return f"(detached HEAD at {self._git(repo.root, 'rev-parse', '--short', 'HEAD').strip()})"

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
        commit = self._commit(repo, commit_hash)
        parents = self._commit_parents(repo, commit)
        if len(parents) > 1:
            args = ["diff", "--no-ext-diff", "--no-textconv", parents[0], commit]
        else:
            args = ["show", "--format=", "--patch", "--no-ext-diff", "--no-textconv", commit]
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
        commit = self._commit(repo, commit_hash)
        parents = self._commit_parents(repo, commit)
        if len(parents) > 1:
            output = self._git(repo.root, "diff", "--name-status", "-z", "-M", parents[0], commit)
        else:
            output = self._git(repo.root, "diff-tree", "--root", "--no-commit-id", "--name-status", "-z", "-r", "-M", commit)
        return json.dumps(self._name_status_records(output), ensure_ascii=False, indent=2)

    def git_grep(
        self,
        repository: str,
        text: str,
        path: str | None = None,
        max_results: int = 50,
    ) -> str:
        if not isinstance(text, str) or not text:
            raise GitWorkspaceError("text darf nicht leer sein.")
        if len(text) > MAX_GREP_TEXT_LENGTH:
            raise GitWorkspaceError(f"text darf höchstens {MAX_GREP_TEXT_LENGTH} Zeichen lang sein.")
        limit = self._grep_limit(max_results)
        repo = self._repo(repository)

        file_args = ["grep", "-l", "-z", "-I", "-F", "-e", text]
        if path is not None:
            file_args += ["--", self._repo_path(repo, path)]
        file_output = self._git(repo.root, *file_args, allowed_returncodes=(0, 1))
        matching_files = [value for value in file_output.split("\x00") if value]

        matches: list[dict[str, object]] = []
        result_size = 0
        truncated = False
        for file_index, filename in enumerate(matching_files):
            remaining = limit - len(matches)
            if remaining <= 0:
                truncated = True
                break
            output = self._git(
                repo.root,
                "grep",
                "-n",
                "-z",
                "-I",
                "-F",
                f"--max-count={remaining + 1}",
                "-e",
                text,
                "--",
                filename,
                allowed_returncodes=(0, 1),
            )
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

        return json.dumps(
            {"matches": matches, "truncated": truncated},
            ensure_ascii=False,
            indent=2,
        )

    def git_blame(
        self,
        repository: str,
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> str:
        repo = self._repo(repository)
        repo_path = self._repo_path(repo, path)
        line_range = self._blame_range(start_line, end_line)
        args = ["blame", "--line-porcelain"]
        if line_range is None:
            args += ["-L", f"1,{MAX_BLAME_LINES + 1}"]
        else:
            args += ["-L", f"{line_range[0]},{line_range[1]}"]
        args += ["--", repo_path]
        records = self._blame_records(self._git(repo.root, *args))
        if line_range is None and len(records) > MAX_BLAME_LINES:
            raise GitWorkspaceError(
                f"Datei hat mehr als {MAX_BLAME_LINES} Zeilen; start_line und end_line müssen angegeben werden."
            )
        return json.dumps(records, ensure_ascii=False, indent=2)
