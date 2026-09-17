from __future__ import annotations

import datetime
import json
import os
import re
import stat
import subprocess
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import islice
from pathlib import Path, PurePath, PureWindowsPath

from .filesystem_security import path_entry_is_symlink_or_reparse

COMMIT_RE = re.compile(r"^[0-9a-fA-F]{4,64}$")
BLAME_TZ_RE = re.compile(r"^([+-])(\d{2})(\d{2})$")
MAX_OUTPUT = 500_000
MAX_GREP_RESULTS = 200
MAX_GREP_TEXT_LENGTH = 4_096
MAX_BLAME_LINES = 200
MAX_OBJECT_DIRECTORIES = 64
MAX_METADATA_ENTRIES = 100_000
GIT_TIMEOUT_SECONDS = 20


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
            dirs[:] = [
                name
                for name in dirs
                if name != ".git" and not path_entry_is_symlink_or_reparse(base / name)
            ]
        found: dict[Path, GitRepository] = {}
        for candidate in candidates:
            try:
                repo = cls._probe(workspace, candidate)
            except GitWorkspaceError:
                continue
            found[repo.root] = repo
        return tuple(
            sorted(found.values(), key=lambda item: item.relative_path.casefold())
        )

    @classmethod
    def _probe(cls, workspace: Path, candidate: Path) -> GitRepository:
        try:
            cls._inside(workspace, candidate.resolve(strict=True), "Repository-Root")
            # Blame reads the worktree mailmap automatically, independently of
            # mailmap.file. It must obey the same boundary as metadata files.
            if os.path.lexists(candidate / ".mailmap"):
                cls._metadata_text(workspace, candidate / ".mailmap")
            # Validate metadata before asking Git to read it. Git follows
            # gitfiles, commondir, refs, indexes and loose-object links itself.
            marker = candidate / ".git"
            if marker.is_dir():
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
            cls._validate_metadata_tree(workspace, expected_common_dir)
            if not expected_git_dir.is_relative_to(expected_common_dir):
                cls._validate_metadata_tree(workspace, expected_git_dir)
            cls._validate_repository_config(
                workspace, candidate, expected_git_dir, expected_common_dir
            )
            root = Path(
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
        if root != candidate.resolve(strict=True):
            raise GitWorkspaceError(
                "Repository-Root stimmt nicht mit dem gefundenen .git-Eintrag überein."
            )
        for label, path in (
            ("Repository-Root", root),
            ("Git-Verzeichnis", git_dir),
            ("Git-Common-Verzeichnis", common_dir),
        ):
            cls._inside(workspace, path, label)
        if git_dir != expected_git_dir or common_dir != expected_common_dir:
            raise GitWorkspaceError("Git-Repositorypfade haben sich während der Prüfung geändert.")

        objects_raw = Path(
            cls._git(candidate, "rev-parse", "--git-path", "objects").removesuffix("\n")
        )
        try:
            objects_dir = (
                objects_raw.resolve(strict=True)
                if objects_raw.is_absolute()
                else (candidate / objects_raw).resolve(strict=True)
            )
        except (OSError, ValueError, RuntimeError) as exc:
            raise GitWorkspaceError(
                "Git-Objektverzeichnis konnte nicht sicher aufgelöst werden."
            ) from exc
        cls._inside(workspace, objects_dir, "Git-Objektverzeichnis")
        cls._validate_alternates(workspace, objects_dir)
        cls._validate_repository_config(
            workspace,
            root,
            git_dir,
            common_dir,
        )
        return GitRepository(
            root,
            git_dir,
            common_dir,
            root.relative_to(workspace).as_posix() or ".",
        )

    @classmethod
    def _metadata_text(cls, workspace: Path, path: Path) -> str:
        """Read a bounded, direct Git control file without path aliases."""
        try:
            cls._inside(workspace, path.resolve(strict=True), "Git-Metadatendatei")
            status = path.lstat()
            if path_entry_is_symlink_or_reparse(path) or not stat.S_ISREG(status.st_mode):
                raise GitWorkspaceError("Git-Metadatendateien müssen reguläre Dateien sein.")
            if status.st_nlink > 1:
                raise GitWorkspaceError("Git-Metadatendateien mit mehreren Hardlinks sind nicht erlaubt.")
            with path.open("rb") as handle:
                data = handle.read(MAX_OUTPUT + 1)
            if len(data) > MAX_OUTPUT:
                raise GitWorkspaceError("Git-Metadatendatei ist zu groß.")
            return data.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise GitWorkspaceError("Git-Metadatendatei konnte nicht sicher gelesen werden.") from exc

    @classmethod
    def _validate_metadata_tree(cls, workspace: Path, directory: Path) -> None:
        """Reject metadata aliases, including object shards and individual refs.

        Checking only the top-level object directory misses links deeper in the
        store. Bound this walk and fail closed if any entry cannot be inspected.
        """
        pending = [directory]
        count = 0
        try:
            while pending:
                current = pending.pop()
                cls._inside(workspace, current.resolve(strict=True), "Git-Metadatenpfad")
                if path_entry_is_symlink_or_reparse(current):
                    raise GitWorkspaceError("Symlinks oder Reparse-Points in Git-Metadaten sind nicht erlaubt.")
                with os.scandir(current) as entries:
                    for entry in entries:
                        count += 1
                        if count > MAX_METADATA_ENTRIES:
                            raise GitWorkspaceError("Zu viele Git-Metadateneinträge für eine sichere Prüfung.")
                        path = Path(entry.path)
                        if path_entry_is_symlink_or_reparse(path):
                            raise GitWorkspaceError("Symlinks oder Reparse-Points in Git-Metadaten sind nicht erlaubt.")
                        status = entry.stat(follow_symlinks=False)
                        if stat.S_ISDIR(status.st_mode):
                            pending.append(path)
                        elif not stat.S_ISREG(status.st_mode):
                            raise GitWorkspaceError("Nicht reguläre Git-Metadatendatei ist nicht erlaubt.")
                        elif status.st_nlink > 1:
                            raise GitWorkspaceError("Git-Metadatendateien mit mehreren Hardlinks sind nicht erlaubt.")
        except OSError as exc:
            raise GitWorkspaceError("Git-Metadaten konnten nicht sicher geprüft werden.") from exc

    @staticmethod
    def _inside(workspace: Path, path: Path, label: str) -> None:
        try:
            path.relative_to(workspace)
        except ValueError as exc:
            raise GitWorkspaceError(
                f"{label} liegt außerhalb des Projekt-Workspaces."
            ) from exc

    @classmethod
    def _validate_alternates(
        cls,
        workspace: Path,
        objects_dir: Path,
        visited: set[Path] | None = None,
    ) -> None:
        # Git follows alternates transitively. Validate the entire graph, with
        # cycle detection and a bound on repository-controlled traversal.
        if visited is None:
            visited = set()
        if objects_dir in visited:
            return
        if len(visited) >= MAX_OBJECT_DIRECTORIES:
            raise GitWorkspaceError("Zu viele Git-Alternate-Objektverzeichnisse.")
        visited.add(objects_dir)
        cls._validate_metadata_tree(workspace, objects_dir)
        alternates = objects_dir / "info" / "alternates"
        if alternates.exists():
            values = cls._metadata_text(workspace, alternates).split("\n")
            for value in values:
                if not value:
                    continue
                # Git C-unquotes these entries. Treating a quoted absolute path
                # as a literal relative directory would validate a different
                # location from the one Git subsequently opens.
                if value.startswith('"'):
                    raise GitWorkspaceError("C-quotierte Git-Alternate-Pfade sind nicht erlaubt.")
                path = Path(value)
                try:
                    resolved = (
                        path.resolve(strict=True)
                        if path.is_absolute()
                        else (objects_dir / path).resolve(strict=True)
                    )
                except (OSError, ValueError, RuntimeError) as exc:
                    raise GitWorkspaceError(
                        "Git-Alternate-Objektverzeichnis ist ungültig oder nicht erreichbar."
                    ) from exc
                cls._inside(
                    workspace,
                    resolved,
                    "Git-Alternate-Objektverzeichnis",
                )
                cls._validate_alternates(workspace, resolved, visited)
        http_alternates = objects_dir / "info" / "http-alternates"
        if http_alternates.exists():
            content = cls._metadata_text(workspace, http_alternates).strip()
            if content:
                raise GitWorkspaceError("Git-HTTP-Alternates sind nicht erlaubt.")

    @classmethod
    def _validate_repository_config(
        cls,
        workspace: Path,
        repo_root: Path,
        git_dir: Path,
        common_dir: Path,
    ) -> None:
        """Reject repository config that can escape the read-only boundary.

        Repository-local filters can execute arbitrary processes while otherwise
        read-only commands inspect the working tree. Local include directives are
        rejected as well because they can import executable filter configuration
        from arbitrary files outside the workspace. The relevant config files
        themselves must be direct, single-link files inside the workspace.
        Blame ignore-revs files are rejected because Git reads configured files
        before processing even an empty --ignore-revs-file reset argument.
        """
        config_paths = {
            common_dir / "config",
            git_dir / "config",
            git_dir / "config.worktree",
        }
        for config_path in config_paths:
            if not config_path.exists():
                continue
            try:
                resolved = config_path.resolve(strict=True)
                cls._inside(workspace, resolved, "Git-Konfigurationsdatei")
                if path_entry_is_symlink_or_reparse(config_path):
                    raise GitWorkspaceError(
                        "Git-Konfigurationsdateien dürfen keine Symlinks oder Reparse-Points sein."
                    )
                status = os.lstat(config_path)
            except GitWorkspaceError:
                raise
            except OSError as exc:
                raise GitWorkspaceError(
                    "Git-Konfigurationsdatei konnte nicht sicher geprüft werden."
                ) from exc
            if stat.S_ISREG(status.st_mode) and status.st_nlink > 1:
                raise GitWorkspaceError(
                    "Git-Konfigurationsdateien mit mehreren Hardlinks sind nicht erlaubt."
                )

            include_output = cls._git(
                repo_root,
                "config",
                "--file",
                str(config_path),
                "--no-includes",
                "--get-regexp",
                r"^include(if\..*)?\.path$",
                allowed_returncodes=(0, 1),
            )
            if include_output.strip():
                raise GitWorkspaceError(
                    "Repository-lokale Git-Config-Includes sind nicht erlaubt."
                )

            filter_output = cls._git(
                repo_root,
                "config",
                "--file",
                str(config_path),
                "--no-includes",
                "--get-regexp",
                r"^filter\..*\.(clean|smudge|process)$|^blame\.ignorerevsfile$",
                allowed_returncodes=(0, 1),
            )
            if filter_output.strip():
                raise GitWorkspaceError(
                    "Repository-lokale Git-Content-Filter oder Blame-Ignore-Dateien sind nicht erlaubt."
                )

    @staticmethod
    def _environment() -> dict[str, str]:
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GIT_")
        }
        env.update(
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_NO_LAZY_FETCH": "1",
                "GIT_ALLOW_PROTOCOL": "",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_LITERAL_PATHSPECS": "1",
                "GIT_PAGER": "cat",
                "PAGER": "cat",
            }
        )
        return env

    @staticmethod
    def _git_command(directory: Path, *args: str) -> list[str]:
        return [
            "git",
            "-C",
            str(directory),
            "--no-pager",
            "-c",
            "color.ui=false",
            "-c",
            "core.quotepath=false",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "submodule.recurse=false",
            "-c",
            "diff.submodule=short",
            "-c",
            "log.showSignature=false",
            "-c",
            f"mailmap.file={os.devnull}",
            "-c",
            "mailmap.blob=",
            "-c",
            f"core.attributesFile={os.devnull}",
            "-c",
            f"core.excludesFile={os.devnull}",
            *args,
        ]

    @classmethod
    def _git(
        cls,
        directory: Path,
        *args: str,
        allowed_returncodes: tuple[int, ...] = (0,),
    ) -> str:
        try:
            result = subprocess.run(
                cls._git_command(directory, *args),
                capture_output=True,
                timeout=GIT_TIMEOUT_SECONDS,
                env=cls._environment(),
                check=False,
            )
        except FileNotFoundError as exc:
            raise GitWorkspaceError(
                "Git ist nicht installiert oder nicht über PATH erreichbar."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise GitWorkspaceError(
                "Git-Aufruf hat das Zeitlimit überschritten."
            ) from exc
        if result.returncode not in allowed_returncodes:
            detail = (
                result.stderr
                or result.stdout
                or f"Exit-Code {result.returncode}".encode("utf-8")
            ).decode("utf-8", errors="replace").strip()
            raise GitWorkspaceError(
                f"Git-Aufruf fehlgeschlagen: {detail[:2000]}"
            )
        if len(result.stdout) > MAX_OUTPUT:
            raise GitWorkspaceError(
                "Git-Ausgabe ist zu groß; Abfrage weiter eingrenzen."
            )
        # Text-mode subprocess pipes translate CR and CRLF into LF, corrupting
        # both source text and filenames in Git's NUL-delimited output.
        return result.stdout.decode("utf-8", errors="replace")

    @classmethod
    @contextmanager
    def _git_null_records(
        cls,
        directory: Path,
        *args: str,
        allowed_returncodes: tuple[int, ...] = (0,),
    ) -> Iterator[Iterator[str]]:
        """Stream bounded NUL records and stop Git when the consumer stops.

        Filename lists may exceed the response limit in total. Each record is
        still bounded, and decoding happens only after a complete record. A
        watchdog also covers blocked pipe reads on Windows. Stderr uses a
        temporary file so an unread stderr pipe cannot deadlock stdout.
        """
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

                def records() -> Iterator[str]:
                    assert process.stdout is not None
                    pending = bytearray()
                    while True:
                        chunk = process.stdout.read1(65_536)
                        if timed_out.is_set():
                            raise GitWorkspaceError("Git-Aufruf hat das Zeitlimit überschritten.")
                        if not chunk:
                            break
                        pending.extend(chunk)
                        while (end := pending.find(b"\x00")) >= 0:
                            if end > MAX_OUTPUT:
                                raise GitWorkspaceError("Git-Dateiname ist zu groß.")
                            value = bytes(pending[:end])
                            del pending[:end + 1]
                            if value:
                                try:
                                    yield value.decode("utf-8")
                                except UnicodeDecodeError as exc:
                                    raise GitWorkspaceError(
                                        "Git-Dateinamen müssen gültiges UTF-8 sein."
                                    ) from exc
                        if len(pending) > MAX_OUTPUT:
                            raise GitWorkspaceError("Git-Dateiname ist zu groß.")
                    process.wait()
                    if timed_out.is_set():
                        raise GitWorkspaceError("Git-Aufruf hat das Zeitlimit überschritten.")
                    if process.returncode not in allowed_returncodes:
                        errors.seek(0)
                        detail = errors.read(2000).decode("utf-8", errors="replace").strip()
                        raise GitWorkspaceError(
                            f"Git-Aufruf fehlgeschlagen: {detail or f'Exit-Code {process.returncode}'}"
                        )
                    if pending:
                        raise GitWorkspaceError("Git-Dateiliste ist unvollständig.")

                iterator = records()
                try:
                    yield iterator
                finally:
                    iterator.close()
                    timer.cancel()
                    if process.poll() is None:
                        process.kill()
                    process.wait()
                    timer.join()
        except FileNotFoundError as exc:
            raise GitWorkspaceError(
                "Git ist nicht installiert oder nicht über PATH erreichbar."
            ) from exc

    def _repo(self, repository: str) -> GitRepository:
        normalized = self._relative(repository, allow_dot=True).as_posix()
        normalized = "." if normalized in {"", "."} else normalized
        for repo in self.repositories:
            if repo.relative_path != normalized:
                continue
            # Discovery grants access to a workspace-relative location, not to
            # cached metadata paths. Gitfiles, common dirs and object stores can
            # all change after startup; probe and validate them again.
            refreshed = self._probe(self.directory, self.directory / normalized)
            if refreshed.relative_path != normalized:
                raise GitWorkspaceError(
                    "Repository-Pfade dürfen nicht über Symlinks oder "
                    "Reparse-Points umgeleitet werden."
                )
            return refreshed
        raise GitWorkspaceError(
            f"Unbekanntes oder nicht freigegebenes Git-Repository: {repository!r}"
        )

    @staticmethod
    def _relative(path: str, *, allow_dot: bool = False) -> PurePath:
        if not isinstance(path, str) or path == "":
            raise GitWorkspaceError("Der Pfad darf nicht leer sein.")
        raw = path
        native, windows = Path(raw), PureWindowsPath(raw)
        pure = PurePath(raw)
        if native.is_absolute() or windows.is_absolute() or windows.drive:
            raise GitWorkspaceError("Der Pfad muss relativ sein.")
        if ".." in pure.parts or ".." in windows.parts:
            raise GitWorkspaceError("Der Pfad darf '..' nicht enthalten.")
        if not allow_dot and pure.as_posix() in {"", "."}:
            raise GitWorkspaceError(
                "Dateipfad darf nicht der Repository-Root sein."
            )
        return pure

    def _repo_path(self, repo: GitRepository, path: str) -> str:
        pure = self._relative(path)
        resolved = (repo.root / Path(pure.as_posix())).resolve(strict=False)
        try:
            resolved.relative_to(repo.root)
            resolved.relative_to(self.directory)
        except ValueError as exc:
            raise GitWorkspaceError(
                "Pfad verweist außerhalb des Repositorys oder Workspaces."
            ) from exc
        return pure.as_posix()

    def _validate_worktree_files(
        self,
        repo: GitRepository,
        path: str | None = None,
    ) -> None:
        """Reject tracked working-tree paths that can alias outside content."""
        args = ["ls-files", "-z"]
        if path is not None:
            args += ["--", path]
        with self._git_null_records(repo.root, *args) as paths:
            for relative in paths:
                pure = self._relative(relative)
                entry = repo.root / Path(pure.as_posix())
                try:
                    current = repo.root
                    for component in pure.parts[:-1]:
                        current /= component
                        if path_entry_is_symlink_or_reparse(current):
                            raise GitWorkspaceError(
                                "Symlinks oder Reparse-Points in getrackten "
                                "Working-Tree-Pfaden sind nicht erlaubt."
                            )
                    status = os.lstat(entry)
                    attributes = int(getattr(status, "st_file_attributes", 0))
                    reparse_flag = int(
                        getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                    )
                    if stat.S_ISLNK(status.st_mode) or attributes & reparse_flag:
                        raise GitWorkspaceError(
                            "Symlinks oder Reparse-Points in getrackten "
                            "Working-Tree-Pfaden sind nicht erlaubt."
                        )
                    if not (
                        stat.S_ISREG(status.st_mode)
                        or stat.S_ISDIR(status.st_mode)
                    ):
                        raise GitWorkspaceError(
                            "Getrackte Working-Tree-Pfade müssen reguläre "
                            "Dateien oder Verzeichnisse sein."
                        )
                    resolved = entry.resolve(strict=False)
                    resolved.relative_to(repo.root)
                    resolved.relative_to(self.directory)
                except GitWorkspaceError:
                    raise
                except FileNotFoundError:
                    # Tracked files may be deleted from the working tree.
                    continue
                except (OSError, ValueError) as exc:
                    raise GitWorkspaceError(
                        "Ein getrackter Working-Tree-Pfad verweist außerhalb des Repositorys oder Workspaces."
                    ) from exc
                if stat.S_ISREG(status.st_mode) and status.st_nlink > 1:
                    raise GitWorkspaceError(
                        "Getrackte Working-Tree-Dateien mit mehreren Hardlinks werden aus Sicherheitsgründen nicht gelesen."
                    )

    @staticmethod
    def _count(value: int) -> int:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= 200
        ):
            raise GitWorkspaceError(
                "max_count muss zwischen 1 und 200 liegen."
            )
        return value

    @staticmethod
    def _grep_limit(value: int) -> int:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= MAX_GREP_RESULTS
        ):
            raise GitWorkspaceError(
                f"max_results muss zwischen 1 und {MAX_GREP_RESULTS} liegen."
            )
        return value

    @staticmethod
    def _blame_range(
        start_line: int | None,
        end_line: int | None,
    ) -> tuple[int, int] | None:
        if start_line is None and end_line is None:
            return None
        if start_line is None or end_line is None:
            raise GitWorkspaceError(
                "start_line und end_line müssen gemeinsam angegeben werden."
            )
        if (
            isinstance(start_line, bool)
            or isinstance(end_line, bool)
            or not isinstance(start_line, int)
            or not isinstance(end_line, int)
        ):
            raise GitWorkspaceError(
                "start_line und end_line müssen ganze Zahlen sein."
            )
        if start_line < 1 or end_line < start_line:
            raise GitWorkspaceError("Ungültiger Blame-Zeilenbereich.")
        if end_line - start_line + 1 > MAX_BLAME_LINES:
            raise GitWorkspaceError(
                f"git_blame darf höchstens {MAX_BLAME_LINES} Zeilen pro Aufruf analysieren."
            )
        return start_line, end_line

    @classmethod
    def _commit(cls, repo: GitRepository, value: str) -> str:
        if (
            not isinstance(value, str)
            or not COMMIT_RE.fullmatch(value.strip())
        ):
            raise GitWorkspaceError(
                "commit_hash muss ein hexadezimaler Git-Commit-Hash sein."
            )
        return cls._git(
            repo.root,
            "rev-parse",
            "--verify",
            f"{value.strip()}^{{commit}}",
        ).strip()

    @classmethod
    def _commit_parents(
        cls,
        repo: GitRepository,
        commit: str,
    ) -> tuple[str, ...]:
        values = cls._git(
            repo.root,
            "rev-list",
            "--parents",
            "-n",
            "1",
            commit,
        ).strip().split()
        if not values or values[0] != commit:
            raise GitWorkspaceError(
                "Commit-Eltern konnten nicht ausgewertet werden."
            )
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
                raise GitWorkspaceError(
                    "Git-Historie konnte nicht ausgewertet werden."
                )
            records.append(
                dict(
                    zip(
                        ("id", "author", "email", "date", "message"),
                        values,
                        strict=True,
                    )
                )
            )
        return records

    @staticmethod
    def _name_status_records(output: str) -> list[dict[str, str]]:
        fields = output.split("\x00")
        if fields and fields[-1] == "":
            fields.pop()
        files: list[dict[str, str]] = []
        labels = {
            "A": "added",
            "C": "copied",
            "D": "deleted",
            "M": "modified",
            "R": "renamed",
            "T": "type_changed",
        }
        index = 0
        while index < len(fields):
            status = fields[index]
            index += 1
            if not status:
                raise GitWorkspaceError(
                    "Commit-Dateiliste konnte nicht ausgewertet werden."
                )
            kind = status[:1]
            if kind in {"R", "C"}:
                if index + 1 >= len(fields):
                    raise GitWorkspaceError(
                        "Commit-Dateiliste konnte nicht ausgewertet werden."
                    )
                old_path = fields[index]
                path = fields[index + 1]
                index += 2
                files.append(
                    {
                        "status": labels.get(kind, "unknown"),
                        "old_path": old_path,
                        "path": path,
                    }
                )
                continue
            if index >= len(fields):
                raise GitWorkspaceError(
                    "Commit-Dateiliste konnte nicht ausgewertet werden."
                )
            path = fields[index]
            index += 1
            files.append(
                {"status": labels.get(kind, "unknown"), "path": path}
            )
        return files

    @staticmethod
    def _grep_records(output: str) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []
        offset = 0
        while offset < len(output):
            path_end = output.find("\x00", offset)
            if path_end < 0:
                raise GitWorkspaceError(
                    "Git-Grep-Ausgabe konnte nicht ausgewertet werden."
                )
            line_end = output.find("\x00", path_end + 1)
            if line_end < 0:
                raise GitWorkspaceError(
                    "Git-Grep-Ausgabe konnte nicht ausgewertet werden."
                )
            text_end = output.find("\n", line_end + 1)
            if text_end < 0:
                text_end = len(output)
            line_text = output[path_end + 1 : line_end]
            try:
                line_number = int(line_text)
            except ValueError as exc:
                raise GitWorkspaceError(
                    "Git-Grep-Zeilennummer konnte nicht ausgewertet werden."
                ) from exc
            records.append(
                {
                    "path": output[offset:path_end],
                    "line": line_number,
                    "text": output[line_end + 1 : text_end],
                }
            )
            offset = text_end + 1
        return records

    @staticmethod
    def _blame_date(timestamp: str, tz_value: str) -> str:
        try:
            timestamp_value = int(timestamp)
        except ValueError as exc:
            raise GitWorkspaceError(
                "Git-Blame-Zeitstempel konnte nicht ausgewertet werden."
            ) from exc
        match = BLAME_TZ_RE.fullmatch(tz_value)
        if match is None:
            raise GitWorkspaceError(
                "Git-Blame-Zeitzone konnte nicht ausgewertet werden."
            )
        sign = 1 if match.group(1) == "+" else -1
        minutes = sign * (
            int(match.group(2)) * 60 + int(match.group(3))
        )
        tzinfo = datetime.timezone(datetime.timedelta(minutes=minutes))
        return datetime.datetime.fromtimestamp(
            timestamp_value,
            tz=datetime.UTC,
        ).astimezone(tzinfo).isoformat()

    @classmethod
    def _blame_records(cls, output: str) -> list[dict[str, object]]:
        lines = output.removesuffix("\n").split("\n") if output else []
        records: list[dict[str, object]] = []
        index = 0
        while index < len(lines):
            header = lines[index].split()
            if len(header) < 3 or not COMMIT_RE.fullmatch(header[0]):
                raise GitWorkspaceError(
                    "Git-Blame-Ausgabe konnte nicht ausgewertet werden."
                )
            try:
                original_line = int(header[1])
                final_line = int(header[2])
            except ValueError as exc:
                raise GitWorkspaceError(
                    "Git-Blame-Zeilennummer konnte nicht ausgewertet werden."
                ) from exc
            commit_hash = header[0]
            index += 1
            metadata: dict[str, str] = {}
            while index < len(lines) and not lines[index].startswith("\t"):
                key, separator, value = lines[index].partition(" ")
                metadata[key] = value if separator else ""
                index += 1
            if index >= len(lines):
                raise GitWorkspaceError(
                    "Git-Blame-Ausgabe enthält keinen Quelltext zur Zeile."
                )
            text = lines[index][1:]
            index += 1
            email = metadata.get("author-mail", "")
            if email.startswith("<") and email.endswith(">"):
                email = email[1:-1]
            if "author-time" not in metadata or "author-tz" not in metadata:
                raise GitWorkspaceError(
                    "Git-Blame-Ausgabe enthält keine vollständigen Autor-Zeitdaten."
                )
            records.append(
                {
                    "line": final_line,
                    "original_line": original_line,
                    "text": text,
                    "author": {
                        "name": metadata.get("author", ""),
                        "email": email,
                    },
                    "date": cls._blame_date(
                        metadata["author-time"],
                        metadata["author-tz"],
                    ),
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
        return json.dumps(
            [{"path": repo.relative_path} for repo in self.repositories],
            ensure_ascii=False,
            indent=2,
        )

    def git_status(self, repository: str) -> str:
        repo = self._repo(repository)
        self._validate_worktree_files(repo)
        output = self._git(
            repo.root,
            "status",
            "--short",
            "--branch",
            "--untracked-files=all",
            "--ignore-submodules=all",
        ).strip()
        return output or "(working tree clean)"

    def git_current_branch(self, repository: str) -> str:
        repo = self._repo(repository)
        try:
            return self._git(
                repo.root,
                "symbolic-ref",
                "--quiet",
                "--short",
                "HEAD",
            ).strip()
        except GitWorkspaceError:
            return (
                "(detached HEAD at "
                f"{self._git(repo.root, 'rev-parse', '--short', 'HEAD').strip()})"
            )

    def git_branches(self, repository: str) -> str:
        output = self._git(
            self._repo(repository).root,
            "branch",
            "--format=%(refname:short)",
        ).strip()
        return output or "(no local branches)"

    def git_diff(
        self,
        repository: str,
        path: str | None = None,
    ) -> str:
        repo = self._repo(repository)
        repo_path = self._repo_path(repo, path) if path is not None else None
        self._validate_worktree_files(repo, repo_path)
        args = ["diff", "--no-ext-diff", "--no-textconv", "--ignore-submodules=all"]
        if repo_path is not None:
            args += ["--", repo_path]
        return self._git(repo.root, *args).strip() or "(no unstaged changes)"

    def git_diff_staged(
        self,
        repository: str,
        path: str | None = None,
    ) -> str:
        repo = self._repo(repository)
        args = ["diff", "--cached", "--no-ext-diff", "--no-textconv"]
        if path is not None:
            args += ["--", self._repo_path(repo, path)]
        return self._git(repo.root, *args).strip() or "(no staged changes)"

    def git_log(self, repository: str, max_count: int = 20) -> str:
        repo = self._repo(repository)
        output = self._git(
            repo.root,
            "log",
            f"--max-count={self._count(max_count)}",
            "--format=%H%x00%an%x00%ae%x00%aI%x00%s%x1e",
        )
        return json.dumps(
            self._records(output),
            ensure_ascii=False,
            indent=2,
        )

    def git_commit_info(self, repository: str, commit_hash: str) -> str:
        repo = self._repo(repository)
        commit = self._commit(repo, commit_hash)
        records = self._records(
            self._git(
                repo.root,
                "show",
                "-s",
                "--format=%H%x00%an%x00%ae%x00%aI%x00%s%x1e",
                commit,
            )
        )
        if len(records) != 1:
            raise GitWorkspaceError(
                "Commit-Metadaten konnten nicht eindeutig ausgewertet werden."
            )
        return json.dumps(records[0], ensure_ascii=False, indent=2)

    def git_commit_diff(
        self,
        repository: str,
        commit_hash: str,
        path: str | None = None,
    ) -> str:
        repo = self._repo(repository)
        commit = self._commit(repo, commit_hash)
        parents = self._commit_parents(repo, commit)
        if len(parents) > 1:
            args = [
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                parents[0],
                commit,
            ]
        else:
            args = [
                "show",
                "--format=",
                "--patch",
                "--no-ext-diff",
                "--no-textconv",
                commit,
            ]
        if path is not None:
            args += ["--", self._repo_path(repo, path)]
        return self._git(repo.root, *args).strip() or (
            "(commit has no textual diff for this selection)"
        )

    def git_file_history(
        self,
        repository: str,
        path: str,
        max_count: int = 20,
    ) -> str:
        repo = self._repo(repository)
        output = self._git(
            repo.root,
            "log",
            "--follow",
            f"--max-count={self._count(max_count)}",
            "--format=%H%x00%an%x00%ae%x00%aI%x00%s%x1e",
            "--",
            self._repo_path(repo, path),
        )
        records = self._records(output)
        result = [
            {
                "author": {
                    "name": item["author"],
                    "email": item["email"],
                },
                "date": item["date"],
                "commit": {
                    "id": item["id"],
                    "message": item["message"],
                },
            }
            for item in records
        ]
        return json.dumps(result, ensure_ascii=False, indent=2)

    def git_commit_files(self, repository: str, commit_hash: str) -> str:
        repo = self._repo(repository)
        commit = self._commit(repo, commit_hash)
        parents = self._commit_parents(repo, commit)
        if len(parents) > 1:
            output = self._git(
                repo.root,
                "diff",
                "--name-status",
                "-z",
                "-M",
                parents[0],
                commit,
            )
        else:
            output = self._git(
                repo.root,
                "diff-tree",
                "--root",
                "--no-commit-id",
                "--name-status",
                "-z",
                "-r",
                "-M",
                commit,
            )
        return json.dumps(
            self._name_status_records(output),
            ensure_ascii=False,
            indent=2,
        )

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
            raise GitWorkspaceError(
                f"text darf höchstens {MAX_GREP_TEXT_LENGTH} Zeichen lang sein."
            )
        limit = self._grep_limit(max_results)
        repo = self._repo(repository)
        repo_path = self._repo_path(repo, path) if path is not None else None
        self._validate_worktree_files(repo, repo_path)

        file_args = ["grep", "-l", "-z", "-I", "-F", "-e", text]
        if repo_path is not None:
            file_args += ["--", repo_path]
        with self._git_null_records(
            repo.root,
            *file_args,
            allowed_returncodes=(0, 1),
        ) as filenames:
            # Every matching file contributes at least one line. One additional
            # filename is sufficient lookahead for the global truncation flag.
            matching_files = list(islice(filenames, limit + 1))

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
                result_size += (
                    len(str(match["path"]))
                    + len(str(match["text"]))
                    + 64
                )
                if result_size > MAX_OUTPUT:
                    raise GitWorkspaceError(
                        "Git-Grep-Ergebnis ist zu groß; Suche weiter eingrenzen."
                    )
                matches.append(match)
            if len(file_matches) > remaining or (
                len(matches) >= limit
                and file_index < len(matching_files) - 1
            ):
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
        self._validate_worktree_files(repo, repo_path)
        line_range = self._blame_range(start_line, end_line)
        args = ["blame", "--line-porcelain", "--no-textconv"]
        if line_range is None:
            try:
                status = (repo.root / repo_path).lstat()
            except OSError as exc:
                raise GitWorkspaceError("Blame-Datei konnte nicht geprüft werden.") from exc
            # Git rejects -L 1,... for an empty file. Still invoke blame without
            # a range so Git verifies that this is a valid tracked file.
            if not (stat.S_ISREG(status.st_mode) and status.st_size == 0):
                args += ["-L", f"1,{MAX_BLAME_LINES + 1}"]
        else:
            args += ["-L", f"{line_range[0]},{line_range[1]}"]
        args += ["--", repo_path]
        records = self._blame_records(self._git(repo.root, *args))
        if line_range is None and len(records) > MAX_BLAME_LINES:
            raise GitWorkspaceError(
                f"Datei hat mehr als {MAX_BLAME_LINES} Zeilen; "
                "start_line und end_line müssen angegeben werden."
            )
        return json.dumps(records, ensure_ascii=False, indent=2)
