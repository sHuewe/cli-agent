"""Bounded and symlink-safe LLM context dump responsibility."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
from typing import Any
from cli_agent.security.filesystem_security import path_entry_is_symlink_or_reparse, regular_file_has_multiple_links
from cli_agent.state.message_visibility import messages_for_model

MAX_DUMP_FILENAME_BYTES = 240
MAX_LEGACY_WINDOWS_DUMP_PATH_CHARS = 240
DUMP_PREFIX_HASH_HEX_CHARS = 16

class ContextDumpMixin:
    @staticmethod
    def _truncate_utf8(value: str, max_bytes: int) -> str:
        encoded = value.encode("utf-8")
        if len(encoded) <= max_bytes:
            return value
        return encoded[:max_bytes].decode("utf-8", errors="ignore")

    def _dump_filename_budget(self, *, windows: bool | None = None) -> int:
        """Bound dumps by the complete legacy Windows path, not only basename.

        A workspace may be nested under a long checkout directory. Keep
        dumps usable without depending on Windows long-path opt-in settings.
        """
        if windows is None:
            windows = os.name == "nt"
        if not windows:
            return MAX_DUMP_FILENAME_BYTES

        dump_dir = str(self.workspace_directory / ".cli-agent")
        # Windows paths are UTF-16; count code units, not Python characters.
        parent_units = len(dump_dir.encode("utf-16-le")) // 2
        return min(
            MAX_DUMP_FILENAME_BYTES,
            MAX_LEGACY_WINDOWS_DUMP_PATH_CHARS - parent_units - 1,
        )

    def _dump_filename(self, filename: str) -> str:
        budget = self._dump_filename_budget()
        filename_bytes = len(filename.encode("utf-8"))
        if filename_bytes > budget:
            raise RuntimeError(
                "LLM-Context-Dump-Dateiname passt nicht zum Workspace-Pfad. "
                f"Der Ablageordner ist {self.workspace_directory / '.cli-agent'}. "
                "Verkürze den Workspace-Pfad oder deaktiviere dump_llm_context."
            )
        if self.dump_file_prefix is None:
            return filename

        candidate = f"{self.dump_file_prefix}_{filename}"
        if len(candidate.encode("utf-8")) <= budget:
            return candidate

        digest = hashlib.sha256(
            self.dump_file_prefix.encode("utf-8")
        ).hexdigest()[:DUMP_PREFIX_HASH_HEX_CHARS]
        tail = f"_{digest}_{filename}"
        available_prefix_bytes = budget - len(tail.encode("utf-8"))
        if available_prefix_bytes <= 0:
            raise RuntimeError(
                "LLM-Context-Dump-Dateiname passt nicht zum Workspace-Pfad. "
                f"Der Ablageordner ist {self.workspace_directory / '.cli-agent'}. "
                "Verkürze den Workspace-Pfad oder deaktiviere dump_llm_context."
            )
        shortened_prefix = self._truncate_utf8(
            self.dump_file_prefix,
            available_prefix_bytes,
        )
        return f"{shortened_prefix}{tail}"

    @staticmethod
    def _ensure_dump_gitignore(dump_directory: Path) -> None:
        gitignore = dump_directory / ".gitignore"
        if path_entry_is_symlink_or_reparse(gitignore):
            raise RuntimeError(
                "LLM-Context-Dump verweigert: .cli-agent/.gitignore darf kein "
                "Symlink oder Reparse Point sein."
            )

        if gitignore.exists():
            if not gitignore.is_file():
                raise RuntimeError(
                    "LLM-Context-Dump verweigert: .cli-agent/.gitignore muss "
                    "eine reguläre Datei sein."
                )
            try:
                if regular_file_has_multiple_links(gitignore):
                    raise RuntimeError(
                        "LLM-Context-Dump verweigert: .cli-agent/.gitignore "
                        "besitzt mehrere Hardlinks."
                    )

                size = gitignore.stat().st_size
                with gitignore.open("rb") as handle:
                    if size > 8192:
                        handle.seek(-8192, os.SEEK_END)
                    tail = handle.read()
                last_line = tail.splitlines()[-1] if tail.splitlines() else b""
                if last_line == b"*":
                    return

                needs_newline = size > 0 and not tail.endswith((b"\n", b"\r"))
                with gitignore.open("ab") as handle:
                    if needs_newline:
                        handle.write(b"\n")
                    handle.write(b"*\n")
                return
            except OSError as exc:
                raise RuntimeError(
                    "LLM-Context-Dump konnte .cli-agent/.gitignore nicht sicher "
                    "prüfen oder aktualisieren."
                ) from exc

        try:
            with gitignore.open("xb") as handle:
                handle.write(b"*\n")
        except FileExistsError:
            # A concurrent creator won the race. Re-run validation so an
            # existing permissive file is never silently accepted.
            ContextDumpMixin._ensure_dump_gitignore(dump_directory)
        except OSError as exc:
            raise RuntimeError(
                "LLM-Context-Dump konnte .cli-agent/.gitignore nicht anlegen."
            ) from exc

    def _safe_dump_path(self, filename: str) -> Path:
        filename = self._dump_filename(filename)
        if Path(filename).name != filename or not filename:
            raise RuntimeError("Ungültiger Dateiname für LLM-Context-Dump.")
        dump_directory = self.workspace_directory / ".cli-agent"
        if dump_directory.exists() and path_entry_is_symlink_or_reparse(dump_directory):
            raise RuntimeError(
                "LLM-Context-Dump verweigert: .cli-agent darf kein Symlink oder Reparse Point sein."
            )
        dump_directory.mkdir(parents=True, exist_ok=True)
        if path_entry_is_symlink_or_reparse(dump_directory):
            raise RuntimeError(
                "LLM-Context-Dump verweigert: .cli-agent darf kein Symlink oder Reparse Point sein."
            )
        self._ensure_dump_gitignore(dump_directory)
        try:
            dump_directory.resolve(strict=True).relative_to(self.workspace_directory)
        except (OSError, RuntimeError, ValueError) as exc:
            raise RuntimeError(
                "LLM-Context-Dump verweigert: .cli-agent liegt nicht sicher im Workspace."
            ) from exc
        dump_file = dump_directory / filename
        if dump_file.exists():
            try:
                if regular_file_has_multiple_links(dump_file):
                    raise RuntimeError(
                        f"LLM-Context-Dump verweigert: {filename!r} besitzt mehrere Hardlinks."
                    )
            except OSError as exc:
                raise RuntimeError(
                    f"LLM-Context-Dump-Datei {filename!r} konnte nicht sicher geprüft werden."
                ) from exc
        if path_entry_is_symlink_or_reparse(dump_file):
            raise RuntimeError(
                f"LLM-Context-Dump verweigert: {filename!r} darf kein Symlink oder Reparse Point sein."
            )
        return dump_file

    def _write_dump_json(self, filename: str, value: Any) -> None:
        self._safe_dump_path(filename).write_text(
            json.dumps(value, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _dump_context(self, working_messages: list[dict[str, Any]], *, phase: str) -> None:
        self._evidence_state.debug_enabled = bool(self.dump_llm_context)
        if not self.dump_llm_context:
            return
        model_history = messages_for_model(self.history)
        history_json = json.dumps(model_history, ensure_ascii=False, indent=2)
        if history_json != self._dumped_history_json:
            self._write_dump_json("history.json", model_history)
            self._dumped_history_json = history_json
        model_messages = (
            self._evidence_state.project(working_messages)
            if phase == "main" and self._evidence_state is not None
            else messages_for_model(working_messages)
        )
        self._write_dump_json(f"{phase}_working_messages.json", model_messages)
        if phase == "main" and self._evidence_state is not None:
            self._write_dump_json("main_full_working_messages.json", messages_for_model(working_messages))
            self._write_dump_json("main_evidence_state.json", self._evidence_state.snapshot())
            if self._evidence_state.debug_updates:
                self._write_dump_json(
                    "main_evidence_update_debug.json",
                    self._evidence_state.debug_updates,
                )
        if model_messages:
            self._write_dump_json(
                f"{phase}_system_prompt.json",
                model_messages[0]["content"],
            )

    def _dump_value(self, filename: str, value: Any) -> None:
        if not self.dump_llm_context:
            return
        self._write_dump_json(filename, value)

    def _remove_dump_value(self, filename: str) -> None:
        if not self.dump_llm_context:
            return
        path = self._safe_dump_path(filename)
        try:
            path.unlink()
        except FileNotFoundError:
            pass

