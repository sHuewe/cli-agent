"""Workspace read/write operations, enforcing the policy methods of Workspace."""
from __future__ import annotations
from cli_agent.platform.workspace_core import *

class WorkspaceIoMixin:
    def _safe_walk_files(self, root: Path, *, max_entries: int | None = None):
        """Yield regular files without following filesystem indirection.

        Every directory entry is counted before filtering so recursive callers
        can bound traversal work even for empty directories, sensitive paths,
        aliases, hardlinks, and other entries that will never be yielded.
        Directory/file symlinks, junction-like paths that resolve elsewhere,
        sensitive paths, and hardlinked files are skipped. This keeps recursive
        read operations within the same security boundary as read_file().
        """
        if root.is_file():
            self._reject_protected_read(root)
            self._reject_hardlinked_file(root)
            yield root
            return
        if not root.is_dir():
            raise WorkspaceError("Suchpfad ist weder Datei noch Ordner.")
        self._reject_protected_read(root)

        pending = [root]
        inspected_entries = 0
        while pending:
            current_path = pending.pop()
            try:
                with os.scandir(current_path) as entries:
                    for item in entries:
                        inspected_entries += 1
                        if max_entries is not None and inspected_entries > max_entries:
                            raise _TraversalBudgetReached

                        entry = Path(item.path)
                        try:
                            resolved = entry.resolve(strict=True)
                            resolved.relative_to(self.directory)
                        except (OSError, ValueError):
                            continue
                        # Do not recursively traverse symlinks, junctions or
                        # other path indirection even when their target happens
                        # to be inside the workspace.
                        if resolved != entry.absolute():
                            continue
                        if self._is_protected_path(resolved):
                            continue
                        if resolved.is_dir():
                            pending.append(resolved)
                            continue
                        if not resolved.is_file():
                            continue
                        try:
                            if regular_file_has_multiple_links(resolved):
                                continue
                        except OSError:
                            continue
                        yield resolved
            except _TraversalBudgetReached:
                raise
            except OSError:
                # Match os.walk's previous default behaviour: inaccessible
                # directories are skipped rather than failing the whole search.
                continue

    def list_files(self, path: str) -> str:
        directory = self.resolve_path(path)
        if not directory.is_dir():
            raise WorkspaceError(f"Pfad ist kein Ordner: {path!r}")
        self._reject_protected_read(directory)

        lines: list[str] = []
        entries = sorted(
            directory.iterdir(),
            key=lambda entry: (not entry.is_dir(), entry.name.lower()),
        )
        for entry in entries:
            if self._is_protected_path(entry):
                continue
            relative = entry.relative_to(self.directory).as_posix()
            kind = "directory" if entry.is_dir() else "file"
            lines.append(f"{kind}\t{relative}")
        return "\n".join(lines) if lines else "(Ordner ist leer)"

    @staticmethod
    def _validate_line_number(value: int | None, *, name: str) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise WorkspaceError(f"{name} muss eine positive ganze Zahl sein.")
        return value

    def _read_text_line_range(
        self,
        file_path: Path,
        *,
        path: str,
        start_line: int | None,
        end_line: int | None,
    ) -> str:
        """Decode UTF-8 or Latin-1 in one bounded scan, without retry I/O.

        Both candidate decoders consume the same bytes. Latin-1 is used only
        after a UTF-8 decode failure, and only if the scanned bytes are
        plausible Latin-1 text. Each candidate buffers at most the output
        limit; neither can cause an extra file read.
        """
        start = 1 if start_line is None else start_line
        utf8_selection = _RangeTextSelection(start, end_line)
        latin1_selection = _RangeTextSelection(start, end_line)
        utf8_decoder = codecs.getincrementaldecoder("utf-8")()
        utf8_invalid = False
        latin1_valid = True
        scanned_bytes = 0
        saw_content = False

        try:
            with file_path.open("rb") as handle:
                while True:
                    remaining = MAX_READ_RANGE_SCAN_BYTES - scanned_bytes
                    raw = handle.read(min(READ_RANGE_CHUNK_BYTES, remaining + 1))
                    if not raw:
                        break
                    saw_content = True
                    scanned_bytes += len(raw)
                    if scanned_bytes > MAX_READ_RANGE_SCAN_BYTES:
                        raise WorkspaceError(
                            "Datei-Ausschnitt überschreitet das Scan-Limit von "
                            f"{MAX_READ_RANGE_SCAN_BYTES} Bytes: {path!r}"
                        )

                    if latin1_valid:
                        try:
                            _validate_latin1_bytes(raw)
                        except UnicodeError:
                            latin1_valid = False
                        else:
                            latin1_selection.feed(raw.decode("iso-8859-1"))

                    if not utf8_invalid:
                        try:
                            decoded = utf8_decoder.decode(raw, final=False)
                        except UnicodeDecodeError:
                            utf8_invalid = True
                        else:
                            utf8_selection.feed(decoded)

                    if utf8_invalid:
                        if not latin1_valid:
                            raise UnicodeError("Datei ist kein unterstützter Text.")
                        if latin1_selection.done:
                            break
                    elif utf8_selection.done:
                        break

                if not utf8_invalid and not utf8_selection.done:
                    try:
                        decoded = utf8_decoder.decode(b"", final=True)
                    except UnicodeDecodeError:
                        utf8_invalid = True
                    else:
                        utf8_selection.feed(decoded, final=True)

                if utf8_invalid:
                    if not latin1_valid:
                        raise UnicodeError("Datei ist kein unterstützter Text.")
                    latin1_selection.feed("", final=True)
                    selected = latin1_selection
                else:
                    selected = utf8_selection

            if selected.oversized:
                raise WorkspaceError(
                    "Angeforderter Datei-Ausschnitt überschreitet "
                    f"das Leselimit von {MAX_READ_FILE_BYTES} Bytes: {path!r}"
                )
            result = "".join(selected.selected_parts)
            if result:
                return result
            if not saw_content:
                return "(Empty file)"
            return "(No lines in requested range)"
        except OSError as exc:
            raise WorkspaceError(
                f"Datei konnte nicht gelesen werden: {path!r}: {exc}"
            ) from exc

    def resolve_readable_file(
        self,
        path: str,
        *,
        direct: bool = False,
    ) -> Path:
        """Resolve and validate one readable regular workspace file.

        This is the shared filesystem-security primitive for built-in MCP
        capabilities that need direct file access without exposing the file
        contents through the OS MCP tool surface.
        """
        # Allowed exceptions within sensitive directories must reject any
        # symlink/junction indirection, including that of a parent component.
        raw = Path(path)
        sensitive_entry = any(part.casefold() in SENSITIVE_DIRECTORY_NAMES for part in raw.parts)
        resolver = self.resolve_direct_path if direct or sensitive_entry else self.resolve_path
        file_path = resolver(path)
        if not file_path.is_file():
            raise WorkspaceError(f"Pfad ist keine Datei: {path!r}")
        self._reject_hardlinked_file(file_path)
        self._reject_protected_read(file_path)
        return file_path

    def resolve_writable_file(self, path: str) -> Path:
        """Resolve and validate one writable regular workspace file path."""
        file_path = self.resolve_direct_path(path, must_exist=False)
        self._reject_protected_mutation(file_path)
        if file_path.exists() and not file_path.is_file():
            raise WorkspaceError(f"Pfad ist keine Datei: {path!r}")
        if file_path.exists():
            self._reject_hardlinked_file(file_path)
        if not file_path.parent.is_dir():
            raise WorkspaceError(
                f"Zielordner existiert nicht: "
                f"{file_path.parent.relative_to(self.directory).as_posix()!r}"
            )
        return file_path

    def read_file(
        self,
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
        start_page: int | None = None,
        end_page: int | None = None,
    ) -> str:
        start_line = self._validate_line_number(
            start_line,
            name="start_line",
        )
        end_line = self._validate_line_number(
            end_line,
            name="end_line",
        )
        if (
            start_line is not None
            and end_line is not None
            and start_line > end_line
        ):
            raise WorkspaceError(
                "start_line darf nicht größer als end_line sein."
            )

        for name, number in (("start_page", start_page), ("end_page", end_page)):
            self._validate_line_number(number, name=name)
        if start_page is not None and end_page is not None and start_page > end_page:
            raise WorkspaceError("start_page darf nicht größer als end_page sein.")
        file_path = self.resolve_readable_file(path)

        ranged_read = start_line is not None or end_line is not None
        if file_path.suffix.lower() == ".pdf":
            if ranged_read:
                raise WorkspaceError(
                    "start_line/end_line werden für PDF-Dateien nicht unterstützt."
                )
            try:
                return read_pdf_text(file_path, start_page=start_page, end_page=end_page, max_pages=self.config.config.get("max_pdf_pages", 100))
            except PdfTextError as exc:
                raise WorkspaceError(str(exc)) from exc
        if start_page is not None or end_page is not None:
            raise WorkspaceError("start_page/end_page sind nur für PDF-Dateien verfügbar.")
        if not self._is_text_file(file_path):
            raise WorkspaceError(
                f"Dateityp darf nicht als Text gelesen werden: {path!r}"
            )

        if ranged_read:
            try:
                return self._read_text_line_range(
                    file_path, path=path, start_line=start_line, end_line=end_line,
                )
            except UnicodeError as exc:
                raise WorkspaceError(
                    f"Datei ist kein unterstützter Text: {path!r}"
                ) from exc

        try:
            if file_path.stat().st_size > MAX_READ_FILE_BYTES:
                raise WorkspaceError(
                    f"Datei überschreitet das Leselimit von "
                    f"{MAX_READ_FILE_BYTES} Bytes: {path!r}"
                )
            data = file_path.read_bytes()
            if len(data) > MAX_READ_FILE_BYTES:
                raise WorkspaceError(
                    f"Datei überschreitet das Leselimit von "
                    f"{MAX_READ_FILE_BYTES} Bytes: {path!r}"
                )
            res, _encoding = _decode_workspace_text(data)
            # Match Path.read_text(newline=None): normalize all text line endings.
            res = res.replace("\r\n", "\n").replace("\r", "\n")
            return res if res else "(Empty file)"
        except UnicodeError as exc:
            raise WorkspaceError(
                f"Datei ist kein unterstützter UTF-8-/ISO-8859-1-Text: {path!r}"
            ) from exc
        except OSError as exc:
            raise WorkspaceError(
                f"Datei konnte nicht gelesen werden: {path!r}: {exc}"
            ) from exc

    def search_text(
        self,
        path: str,
        text: str,
        max_results: int = 50,
    ) -> str:
        if not isinstance(text, str) or not text:
            raise WorkspaceError("text darf nicht leer sein.")
        if "\n" in text or "\r" in text:
            raise WorkspaceError("text darf keine Zeilenumbrüche enthalten.")
        if len(text) > MAX_SEARCH_TEXT_LENGTH:
            raise WorkspaceError(
                f"text darf höchstens {MAX_SEARCH_TEXT_LENGTH} Zeichen lang sein."
            )
        limit = self._limit(max_results, name="max_results", maximum=MAX_SEARCH_RESULTS)
        root = self.resolve_direct_path(path)
        matches: list[dict[str, object]] = []
        truncated = False
        scan_budget_reached = False
        scanned_files = 0
        scanned_bytes = 0

        try:
            for file_path in self._safe_walk_files(root, max_entries=MAX_SEARCH_SCAN_FILES):
                if scanned_files >= MAX_SEARCH_SCAN_FILES:
                    truncated = True
                    scan_budget_reached = True
                    break
                scanned_files += 1
                if not self._is_text_file(file_path):
                    continue

                remaining_bytes = MAX_SEARCH_SCAN_BYTES - scanned_bytes
                if remaining_bytes <= 0:
                    truncated = True
                    scan_budget_reached = True
                    break

                try:
                    with file_path.open("rb") as handle:
                        data = handle.read(min(MAX_READ_FILE_BYTES, remaining_bytes) + 1)
                    scanned_bytes += len(data)
                    if scanned_bytes > MAX_SEARCH_SCAN_BYTES:
                        truncated = True
                        scan_budget_reached = True
                        break
                    if len(data) > MAX_READ_FILE_BYTES:
                        continue
                    # Decode the complete bounded file before publishing any
                    # matches. A later invalid byte must invalidate the entire
                    # file rather than leave earlier partial results behind.
                    content, _encoding = _decode_workspace_text(data)
                    with io.StringIO(content, newline=None) as handle:
                        for line_number, line in enumerate(handle, start=1):
                            match_start = line.find(text)
                            if match_start < 0:
                                continue
                            line_text = line.rstrip("\r\n")
                            # Keep the whole literal match, including queries up to
                            # MAX_SEARCH_TEXT_LENGTH, and centre context around it.
                            width = max(MAX_SEARCH_LINE_CHARS, len(text))
                            text_truncated = len(line_text) > width
                            excerpt_start = 0
                            if text_truncated:
                                excerpt_start = min(
                                    max(0, match_start - (width - len(text)) // 2),
                                    len(line_text) - width,
                                )
                                line_text = line_text[excerpt_start:excerpt_start + width]
                            match: dict[str, object] = {
                                "path": file_path.relative_to(self.directory).as_posix(),
                                "line": line_number,
                                "column": match_start + 1,
                                "text": line_text,
                            }
                            if text_truncated:
                                match["text_truncated"] = True
                                match["text_start_column"] = excerpt_start + 1
                            matches.append(match)
                            # Probe for one additional result so truncated is true
                            # only when a result was actually omitted.
                            if len(matches) > limit:
                                truncated = True
                                break
                except UnicodeError:
                    continue
                except OSError as exc:
                    raise WorkspaceError(
                        f"Datei konnte bei der Textsuche nicht sicher gelesen werden: "
                        f"{file_path.name!r}: {exc}"
                    ) from exc
                if truncated:
                    break
        except _TraversalBudgetReached:
            truncated = True
            scan_budget_reached = True

        result: dict[str, object] = {
            "matches": matches[:limit],
            "truncated": truncated,
        }
        if scan_budget_reached:
            result["truncation_reason"] = "scan_budget"
        return json.dumps(result, ensure_ascii=False, indent=2)

    def find_files(
        self,
        path: str,
        pattern: str,
        max_results: int = 100,
    ) -> str:
        if not isinstance(pattern, str) or not pattern:
            raise WorkspaceError("pattern darf nicht leer sein.")
        limit = self._limit(max_results, name="max_results", maximum=MAX_FIND_RESULTS)
        root = self.resolve_direct_path(path)
        matches: list[str] = []
        truncated = False
        scan_budget_reached = False
        scanned_files = 0

        try:
            for file_path in self._safe_walk_files(root, max_entries=MAX_FIND_SCAN_FILES):
                if scanned_files >= MAX_FIND_SCAN_FILES:
                    truncated = True
                    scan_budget_reached = True
                    break
                scanned_files += 1
                relative = file_path.relative_to(self.directory).as_posix()
                if not (
                    fnmatch.fnmatchcase(file_path.name, pattern)
                    or fnmatch.fnmatchcase(relative, pattern)
                ):
                    continue
                matches.append(relative)
                # Probe for one additional result so exact-limit result sets are
                # reported as complete.
                if len(matches) > limit:
                    truncated = True
                    break
        except _TraversalBudgetReached:
            truncated = True
            scan_budget_reached = True

        result: dict[str, object] = {
            "files": matches[:limit],
            "truncated": truncated,
        }
        if scan_budget_reached:
            result["truncation_reason"] = "scan_budget"
        return json.dumps(result, ensure_ascii=False, indent=2)

    def file_info(self, path: str) -> str:
        resolved = self.resolve_path(path)
        self._reject_protected_read(resolved)
        if resolved.is_file():
            self._reject_hardlinked_file(resolved)
            kind = "file"
        elif resolved.is_dir():
            kind = "directory"
        else:
            raise WorkspaceError(f"Pfad ist keine reguläre Datei oder Ordner: {path!r}")
        try:
            stat = resolved.stat()
        except OSError as exc:
            raise WorkspaceError(
                f"Dateimetadaten konnten nicht gelesen werden: {path!r}: {exc}"
            ) from exc
        result = {
            "path": resolved.relative_to(self.directory).as_posix() or ".",
            "type": kind,
            "size_bytes": stat.st_size if kind == "file" else None,
            "modified": datetime.datetime.fromtimestamp(
                stat.st_mtime,
                tz=datetime.UTC,
            ).isoformat(),
        }
        return json.dumps(result, ensure_ascii=False, indent=2)

    def delete_file(self, path: str) -> str:
        file_path = self.resolve_direct_path(path)
        if not file_path.is_file():
            raise WorkspaceError(f"Pfad ist keine Datei: {path!r}")
        self._reject_hardlinked_file(file_path)
        self._reject_protected_mutation(file_path)

        try:
            file_path.unlink()
        except OSError as exc:
            raise WorkspaceError(
                f"Datei konnte nicht gelöscht werden: {path!r}: {exc}"
            ) from exc

        relative = file_path.relative_to(self.directory).as_posix()
        return f"Datei gelöscht: {relative}"

    def copy_file(self, path_src: str, path_dst: str) -> str:
        src_path = self.resolve_direct_path(path_src)
        if not src_path.is_file():
            raise WorkspaceError(f"Quellpfad ist keine Datei: {path_src!r}")
        self._reject_hardlinked_file(src_path)
        self._reject_protected_read(src_path)

        dst_path = self.resolve_direct_path(path_dst, must_exist=False)
        self._reject_protected_mutation(dst_path)
        if dst_path.exists() and not dst_path.is_file():
            raise WorkspaceError(f"Zielpfad ist keine Datei: {path_dst!r}")
        if dst_path.exists():
            self._reject_hardlinked_file(dst_path)
        if not dst_path.parent.is_dir():
            raise WorkspaceError(
                f"Zielordner existiert nicht: "
                f"{dst_path.parent.relative_to(self.directory).as_posix()!r}"
            )

        try:
            shutil.copy2(src_path, dst_path)
        except OSError as exc:
            raise WorkspaceError(
                f"Datei konnte nicht kopiert werden: {path_src!r} -> {path_dst!r}: {exc}"
            ) from exc

        relative = dst_path.relative_to(self.directory).as_posix()
        return f"Datei kopiert: {relative} "

    def move_file(self, path_src: str, path_dst: str) -> str:
        src_path = self.resolve_direct_path(path_src)
        if not src_path.is_file():
            raise WorkspaceError(f"Quellpfad ist keine Datei: {path_src!r}")
        self._reject_hardlinked_file(src_path)
        self._reject_protected_mutation(src_path)

        dst_path = self.resolve_direct_path(path_dst, must_exist=False)
        self._reject_protected_mutation(dst_path)
        if dst_path.exists() and not dst_path.is_file():
            raise WorkspaceError(f"Zielpfad ist keine Datei: {path_dst!r}")
        if dst_path.exists():
            self._reject_hardlinked_file(dst_path)
        if not dst_path.parent.is_dir():
            raise WorkspaceError(
                f"Zielordner existiert nicht: {path_dst!r}"
            )
        if src_path == dst_path:
            relative = src_path.relative_to(self.directory).as_posix()
            return f"Datei befindet sich bereits am Ziel: {relative}"

        try:
            src_path.replace(dst_path)
        except OSError as exc:
            raise WorkspaceError(
                f"Datei konnte nicht verschoben werden: {path_src!r} -> {path_dst!r}: {exc}"
            ) from exc

        relative = dst_path.relative_to(self.directory).as_posix()
        return f"Datei verschoben: {relative}"

    def make_directory(self, path: str) -> str:
        dir_path = self.resolve_direct_path(path, must_exist=False)
        self._reject_protected_mutation(dir_path)
        if dir_path.exists() and not dir_path.is_dir():
            raise WorkspaceError(f"Pfad ist kein Ordner: {path!r}")
        if dir_path.exists():
            relative = dir_path.relative_to(self.directory).as_posix()
            return f"Ordner existiert bereits: {relative!r}"

        try:
            dir_path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise WorkspaceError(
                f"Ordner konnte nicht erstellt werden: {path!r}: {exc}"
            ) from exc

        relative = dir_path.relative_to(self.directory).as_posix()
        return f"Ordner erstellt: {relative}"

    def write_file(self, path: str, content: str) -> str:
        file_path = self.resolve_writable_file(path)
        if not self._is_text_file(file_path):
            raise WorkspaceError(
                f"Dateityp darf nicht als Text geschrieben werden: {path!r}"
            )

        try:
            newline = self._existing_line_ending(file_path)
            encoding = "utf-8"
            if file_path.exists():
                _previous_content, encoding = _decode_workspace_text(
                    file_path.read_bytes()
                )
            normalized_content = content.replace("\r\n", "\n").replace("\r", "\n")
            # Validate representability before opening the target for writing:
            # a failed Latin-1 encode must never truncate an existing file.
            normalized_content.replace("\n", newline).encode(encoding)
            file_path.write_text(
                normalized_content,
                encoding=encoding,
                newline=newline,
            )
        except UnicodeError as exc:
            raise WorkspaceError(
                f"Datei kann nicht in ihrer bisherigen Kodierung geschrieben werden: "
                f"{path!r}"
            ) from exc
        except OSError as exc:
            raise WorkspaceError(
                f"Datei konnte nicht geschrieben werden: {path!r}: {exc}"
            ) from exc

        relative = file_path.relative_to(self.directory).as_posix()
        return f"Datei geschrieben: {relative} ({file_path.stat().st_size} Bytes)"
