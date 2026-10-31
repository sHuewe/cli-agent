from __future__ import annotations

import asyncio
import importlib
import difflib
import os
import stat
import secrets
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from markdown_it import MarkdownIt

from cli_agent.approval_display import approval_arguments
from cli_agent.local_commands import classify_local_command
from cli_agent.os_operations import Workspace, WorkspaceError
from cli_agent.terminal_output import sanitize_terminal_text

MAX_APPROVAL_DIFF_BYTES = 128_000
MAX_APPROVAL_DIFF_CHARS = 30_000




def _open_preview_file(path: Path):
    """Open a preview target without following a final symlink or reparse point.

    Returns (binary_handle, Windows volume/file identifier or None). Windows
    uses handle metadata rather than potentially missing/non-unique st_ino.
    """
    if os.name != "nt":
        if not hasattr(os, "O_NOFOLLOW"):
            raise WorkspaceError("Sicheres Öffnen wird auf diesem System nicht unterstützt.")
        flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(path, flags)
        return os.fdopen(descriptor, "rb"), None

    import ctypes
    import msvcrt
    from ctypes import wintypes

    class _FileInfo(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    class _FileId128(ctypes.Structure):
        _fields_ = [("Identifier", ctypes.c_ubyte * 16)]

    class _FileIdInfo(ctypes.Structure):
        _fields_ = [
            ("VolumeSerialNumber", ctypes.c_ulonglong),
            ("FileId", _FileId128),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    get_info = kernel.GetFileInformationByHandle
    get_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(_FileInfo)]
    get_info.restype = wintypes.BOOL
    get_file_id = kernel.GetFileInformationByHandleEx
    get_file_id.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ]
    get_file_id.restype = wintypes.BOOL
    get_path = kernel.GetFinalPathNameByHandleW
    get_path.argtypes = [
        wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
    ]
    get_path.restype = wintypes.DWORD
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL

    # OPEN_REPARSE_POINT makes the final symlink itself the opened object;
    # never follow it to a protected file. BACKUP_SEMANTICS permits checking
    # directory/reparse handles which are rejected below.
    handle = create(
        str(path), 0x80000000, 0x00000007, None, 3,
        0x00200000 | 0x02000000, None,
    )
    if handle == ctypes.c_void_p(-1).value:
        raise OSError(ctypes.get_last_error(), "Sicheres Öffnen fehlgeschlagen")
    owned = True
    try:
        info = _FileInfo()
        if not get_info(handle, ctypes.byref(info)):
            raise OSError(ctypes.get_last_error(), "Dateiidentität nicht lesbar")
        # The older 64-bit file index is not unique on ReFS. FileIdInfo (18)
        # returns a full 128-bit ID and volume serial from the open handle.
        # Do not fall back to BY_HANDLE_FILE_INFORMATION IDs on failure.
        file_id_info = _FileIdInfo()
        if not get_file_id(
            handle, 18, ctypes.byref(file_id_info), ctypes.sizeof(file_id_info)
        ):
            raise WorkspaceError(
                "128-Bit-Dateiidentität konnte nicht sicher geprüft werden."
            )
        file_id = bytes(file_id_info.FileId.Identifier)
        if (
            (int(info.dwFileAttributes) & (0x400 | 0x10))
            or info.nNumberOfLinks != 1
            or file_id == bytes(16)
        ):
            raise WorkspaceError("Dateiidentität konnte nicht sicher geprüft werden.")

        buffer = ctypes.create_unicode_buffer(32768)
        length = get_path(handle, buffer, len(buffer), 0)
        if not length or length >= len(buffer):
            raise WorkspaceError("Tatsächlicher Dateipfad konnte nicht geprüft werden.")

        # Final handle paths have the \\?\ prefix. Compare the physical path
        # from the open handle rather than a pathname that can be swapped.
        final_path = buffer.value
        if final_path.startswith("\\\\?\\UNC\\"):
            final_path = "\\\\" + final_path[8:]
        elif final_path.startswith("\\\\?\\"):
            final_path = final_path[4:]
        if os.path.normcase(os.path.normpath(final_path)) != os.path.normcase(
            os.path.normpath(str(path))
        ):
            raise WorkspaceError("Zieldatei verweist auf einen anderen Dateipfad.")

        identifier = (int(file_id_info.VolumeSerialNumber), file_id)
        descriptor = msvcrt.open_osfhandle(
            int(handle), os.O_RDONLY | getattr(os, "O_BINARY", 0)
        )
        owned = False  # CRT descriptor now owns the Windows handle.
        try:
            return os.fdopen(descriptor, "rb"), identifier
        except BaseException:
            os.close(descriptor)
            raise
    finally:
        if owned:
            close(handle)

def _preview_target_bytes(
    workspace: Workspace, relative_path: str,
) -> tuple[Path, bytes | None]:
    """Read at most the preview budget, verifying the opened file's identity.

    The workspace resolver protects target paths, parents, excluded paths and
    hardlinks. On POSIX require O_NOFOLLOW and reliable inode identifiers.
    On Windows use a no-follow Win32 handle, full 128-bit ID, and final path.
    A failed validation never returns any file content to the browser.
    """
    target = workspace.resolve_writable_file(relative_path)
    if not workspace._is_text_file(target):
        raise WorkspaceError("Dateityp ist nicht zum Schreiben freigegeben.")
    try:
        before = target.lstat()
    except FileNotFoundError:
        # A new file has no previous bytes. Revalidate in case the parent/path
        # was exchanged while checking its existence.
        workspace.resolve_writable_file(relative_path)
        if target.is_symlink() or target.exists():
            raise WorkspaceError("Zieldatei wurde während der Vorschau verändert.")
        return target, None

    if not stat.S_ISREG(before.st_mode):
        raise WorkspaceError("Zieldatei ist keine reguläre Datei.")
    if before.st_size > MAX_APPROVAL_DIFF_BYTES:
        raise WorkspaceError("Originaldatei ist zu groß für die Vorschau.")

    try:
        handle, windows_id = _open_preview_file(target)
        with handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                raise WorkspaceError("Dateiidentität konnte nicht sicher geprüft werden.")
            if os.name != "nt":
                # st_ino=0 or st_dev=0 may be dummy/non-unique identifiers
                # (e.g. virtual filesystems). Fail closed rather than trust them.
                if (
                    before.st_ino <= 0 or opened.st_ino <= 0
                    or before.st_dev <= 0 or opened.st_dev <= 0
                ):
                    raise WorkspaceError("Dateiidentität ist nicht verfügbar.")
                if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                    raise WorkspaceError("Zieldatei wurde während der Vorschau ausgetauscht.")
            elif windows_id is None:
                raise WorkspaceError("Windows-Dateiidentität ist nicht verfügbar.")
            if opened.st_size > MAX_APPROVAL_DIFF_BYTES:
                raise WorkspaceError("Originaldatei ist zu groß für die Vorschau.")

            if workspace.resolve_writable_file(relative_path) != target:
                raise WorkspaceError("Zieldatei wurde während der Vorschau ausgetauscht.")
            data = handle.read(MAX_APPROVAL_DIFF_BYTES + 1)
            if len(data) > MAX_APPROVAL_DIFF_BYTES:
                raise WorkspaceError("Originaldatei ist zu groß für die Vorschau.")

            after = os.fstat(handle.fileno())
            checked = workspace.resolve_writable_file(relative_path)
            path_stat = target.lstat()
            if (
                checked != target
                or not stat.S_ISREG(path_stat.st_mode)
                or (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                   != (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
            ):
                raise WorkspaceError("Zieldatei wurde während der Vorschau verändert.")
            if os.name == "nt":
                # Check the same physical Windows file is still at the target.
                # A symlink/reparse swap is rejected by _open_preview_file.
                recheck_handle, recheck_id = _open_preview_file(target)
                with recheck_handle:
                    if recheck_id != windows_id:
                        raise WorkspaceError(
                            "Zieldatei wurde während der Vorschau ausgetauscht."
                        )
            elif (
                (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)
                or (path_stat.st_dev, path_stat.st_ino) != (opened.st_dev, opened.st_ino)
            ):
                raise WorkspaceError("Zieldatei wurde während der Vorschau ausgetauscht.")
            return target, data
    except (OSError, FileNotFoundError) as exc:
        raise WorkspaceError("Datei konnte nicht sicher für die Vorschau gelesen werden.") from exc


def _preview_line_ending(original: bytes | None) -> str:
    """Match Workspace._existing_line_ending without an unbounded second read."""
    if original is None:
        return "\n"
    crlf = original.count(b"\r\n")
    lf = original.count(b"\n") - crlf
    cr = original.count(b"\r") - crlf
    if crlf > lf and crlf > cr:
        return "\r\n"
    if cr > lf and cr > crlf:
        return "\r"
    return "\n"

_MARKDOWN = (
    MarkdownIt(
        "commonmark",
        {
            "html": False,
            "linkify": False,
        },
    )
    .enable("table")
    .enable("strikethrough")
)


def _render_markdown(text: str) -> str:
    """Render trusted Markdown output while keeping raw HTML disabled."""

    return _MARKDOWN.render(text)


_WEB_EXTRA_ERROR = (
    "Die Web-UI ist nicht installiert. Installiere cli-agent mit dem optionalen "
    "Extra 'web', z. B. mit: pipx install 'cli-agent[web]'"
)

_WEB_UI_COMMANDS: dict[str, tuple[int, str]] = {
    "tokens": (0, "tokens"),
    "clear_web_context": (0, "clear_web_context"),
    "add_web_context": (1, "add_web_context <URL>"),
    "enable": (1, "enable <SERVER>"),
    "disable": (1, "disable <SERVER>"),
}


def _web_ui_command_prompt(command: object, argument: object = None) -> str:
    """Build a validated local command without ever falling through to the LLM."""

    if not isinstance(command, str) or command not in _WEB_UI_COMMANDS:
        raise ValueError("Unbekannter Web-UI-Befehl.")

    argument_count, usage = _WEB_UI_COMMANDS[command]
    if argument_count == 0:
        if argument not in (None, ""):
            raise ValueError(f"Ungültige Syntax. Verwendung: {usage}")
        prompt = command
    else:
        if not isinstance(argument, str) or not argument.strip():
            raise ValueError(f"Argument fehlt. Verwendung: {usage}")
        prompt = f"{command} {argument.strip()}"

    parsed = classify_local_command(prompt)
    if (
        not parsed.is_local
        or parsed.error is not None
        or parsed.command != command
    ):
        raise ValueError(f"Ungültiges Argument. Verwendung: {usage}")
    return prompt


def _load_web_dependencies() -> dict[str, Any]:
    try:
        starlette_applications = importlib.import_module("starlette.applications")
        starlette_middleware = importlib.import_module("starlette.middleware")
        starlette_responses = importlib.import_module("starlette.responses")
        starlette_routing = importlib.import_module("starlette.routing")
        uvicorn = importlib.import_module("uvicorn")
    except ModuleNotFoundError as exc:
        if (exc.name or "").split(".", 1)[0] in {
            "starlette",
            "uvicorn",
        }:
            raise RuntimeError(_WEB_EXTRA_ERROR) from exc
        raise

    return {
        "Starlette": starlette_applications.Starlette,
        "Middleware": starlette_middleware.Middleware,
        "HTMLResponse": starlette_responses.HTMLResponse,
        "PlainTextResponse": starlette_responses.PlainTextResponse,
        "Route": starlette_routing.Route,
        "uvicorn": uvicorn,
    }


def ensure_web_ui_available() -> None:
    """Fail before model/MCP startup when the optional web extra is absent."""

    _load_web_dependencies()


class WebUiApprovalBroker:
    """Bridge the agent's approval callback to one authenticated browser."""

    def __init__(self, workspace: Workspace | None = None) -> None:
        self._workspace = workspace
        self._approval_snapshots: dict[str, tuple[str, bytes | None]] = {}
        self._blocked_approvals: set[str] = set()
        self._sender: Callable[[dict[str, Any]], Awaitable[None]] | None = None
        self._event_sender: Callable[[dict[str, Any]], Awaitable[None]] | None = None
        self._pending: dict[str, asyncio.Future[bool | str]] = {}

    def set_event_sender(
        self,
        sender: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        self._event_sender = sender

    def attach(
        self,
        sender: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        self._sender = sender

    def detach(
        self,
        sender: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if self._sender is not sender:
            return
        self._sender = None
        self.deny_all()

    def deny_all(self) -> None:
        self._approval_snapshots.clear()
        self._blocked_approvals.clear()
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_result(False)

    def is_pending(self, request_id: str) -> bool:
        """Whether a request can still be decided by the current UI."""
        future = self._pending.get(request_id)
        return future is not None and not future.done()

    def resolve(self, request_id: str, decision: str) -> bool:
        future = self._pending.get(request_id)
        if future is None or future.done() or decision not in {"no", "yes", "session"}:
            return False
        if request_id in self._blocked_approvals:
            # The browser can only dismiss this dialog. Ignore forged approvals.
            future.set_result(False)
            return True
        snapshot = self._approval_snapshots.get(request_id)
        if snapshot is not None and decision in {"yes", "session"}:
            relative_path, previous = snapshot
            try:
                assert self._workspace is not None
                _path, current = _preview_target_bytes(self._workspace, relative_path)
            except (WorkspaceError, OSError):
                future.set_result(False)
                return True
            if current != previous:
                future.set_result(False)
                return True
        if decision == "session":
            future.set_result("session")
        else:
            future.set_result(decision == "yes")
        return True

    async def show_mcp_user_message(
        self,
        content: str,
        source: str | None,
    ) -> None:
        sender = self._event_sender
        if sender is None:
            return
        safe_content = sanitize_terminal_text(
            content,
            multiline=True,
            escape_invisible_formatting=True,
            escape_literal_backslashes=True,
        )
        safe_source = (
            sanitize_terminal_text(
                source,
                multiline=False,
                escape_invisible_formatting=True,
                escape_literal_backslashes=True,
            )
            if source
            else None
        )
        await sender(
            {
                "type": "mcp_user_message",
                "content": safe_content,
                "source": safe_source,
            }
        )

    def _write_preview(
        self, tool_name: str, arguments: dict[str, object],
    ) -> tuple[dict[str, object], tuple[Path, bytes | None] | None]:
        # Do not give external MCP tools access to the local workspace.
        if tool_name != "os__write_file" or self._workspace is None:
            return {}, None
        path = arguments.get("path")
        content = arguments.get("content")
        if not isinstance(path, str) or not isinstance(content, str):
            return {"diff_error": "Ungültige Schreibargumente."}, None
        try:
            _target, original = _preview_target_bytes(self._workspace, path)
            from cli_agent.os_operations import _decode_workspace_text
            before, encoding = (
                _decode_workspace_text(original) if original is not None else ("", "utf-8")
            )
            newline = _preview_line_ending(original)
            proposed = content.replace("\r\n", "\n").replace("\r", "\n")
            proposed.replace("\n", newline).encode(encoding)
            if len(proposed.encode("utf-8")) > MAX_APPROVAL_DIFF_BYTES:
                return {"diff_error": "Neuer Dateiinhalt ist zu groß für die Vorschau."}, None

            # write_file retains the original newline convention, so compare
            # logical lines rather than treating CRLF/CR as content changes.
            before = before.replace("\r\n", "\n").replace("\r", "\n")

            # Browsers interpret bidi/format controls even in textContent.
            # Escape them (and literal backslashes for unambiguous display)
            # before producing the human-facing diff, never before writing.
            safe_before = sanitize_terminal_text(
                before, multiline=True, escape_invisible_formatting=True,
                escape_literal_backslashes=True,
            )
            safe_after = sanitize_terminal_text(
                proposed, multiline=True, escape_invisible_formatting=True,
                escape_literal_backslashes=True,
            )
            safe_path = sanitize_terminal_text(
                path, multiline=False, escape_invisible_formatting=True,
                escape_literal_backslashes=True,
            )
            records = difflib.unified_diff(
                safe_before.splitlines(keepends=True),
                safe_after.splitlines(keepends=True),
                fromfile=f"a/{safe_path}" if original is not None else "/dev/null",
                tofile=f"b/{safe_path}",
            )
            # difflib leaves a final unterminated content record without a
            # line separator. Append one plus the conventional explicit marker
            # so -old and +new remain distinct, including newline-only edits.
            diff_parts: list[str] = []
            for record in records:
                diff_parts.append(record)
                if not record.endswith("\n"):
                    diff_parts.append("\n\\ No newline at end of file\n")
            diff = "".join(diff_parts)
            if len(diff) > MAX_APPROVAL_DIFF_CHARS:
                return {
                    "diff_error": "Diff zu groß: vollständige Prüfung nicht möglich.",
                }, None
            return {"diff": diff, "diff_status": "created" if original is None else "modified"}, (path, original)
        except (WorkspaceError, OSError, UnicodeError, ValueError) as exc:
            return {"diff_error": str(exc)}, None

    async def approve_tool_call(
        self,
        tool_name: str,
        arguments: dict[str, object],
    ) -> bool | str:
        sender = self._sender
        if sender is None:
            # The browser is the only interactive approval channel in Web-UI
            # mode. If it is absent or disconnected, fail closed.
            return False

        request_id = secrets.token_urlsafe(18)
        future: asyncio.Future[bool | str] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending[request_id] = future
        safe_tool_name = sanitize_terminal_text(
            tool_name,
            multiline=False,
            escape_invisible_formatting=True,
            escape_literal_backslashes=True,
        )
        preview, snapshot = self._write_preview(tool_name, arguments)
        blocked = (
            tool_name == "os__write_file"
            and self._workspace is not None
            and snapshot is None
        )
        if blocked:
            self._blocked_approvals.add(request_id)
        if snapshot is not None:
            self._approval_snapshots[request_id] = snapshot
        try:
            await sender(
                {
                    "type": "approval_required",
                    "id": request_id,
                    "tool": safe_tool_name,
                    "arguments": approval_arguments(arguments),
                    **preview,
                    "diff_blocked": blocked,
                }
            )
        except Exception:
            self._pending.pop(request_id, None)
            self._approval_snapshots.pop(request_id, None)
            self._blocked_approvals.discard(request_id)
            return False

        try:
            return await future
        finally:
            self._pending.pop(request_id, None)
            self._approval_snapshots.pop(request_id, None)
            self._blocked_approvals.discard(request_id)


INDEX_HTML = """<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>cli-agent</title>
  <link rel="stylesheet" href="/assets/app.css">
  <script src="/assets/app.js" defer></script>
</head>
<body>
  <main class="shell">
    <header>
      <div>
        <h1>cli-agent</h1>
        <div id="session-meta" class="meta">Verbindung wird hergestellt …</div>
      </div>
      <div class="header-actions">
        <button id="new-session" class="secondary" type="button">Neue Session in neuem Tab</button>
        <button id="list-sessions" class="secondary" type="button">Sessions</button>
        <button id="reset-history" class="secondary" type="button">History zurücksetzen</button>
        <button id="show-working-context" class="secondary" type="button">LLM Context</button>
        <button id="quit" class="secondary" type="button">Session beenden</button>
      </div>
    </header>
    <section id="chat" class="chat" aria-live="polite"></section>
    <div id="commands" class="commands" aria-label="Lokale Befehle">
      <button class="secondary" data-command="tokens" type="button">Tokens</button>
      <button class="secondary" data-command="add_web_context" type="button">Web-Kontext hinzufügen</button>
      <button class="secondary" data-command="clear_web_context" type="button">Web-Kontext löschen</button>
      <button class="secondary" data-command="enable" type="button">MCP aktivieren</button>
      <button class="secondary" data-command="disable" type="button">MCP deaktivieren</button>
      <button id="cancel-command" class="secondary hidden" type="button">Abbrechen</button>
    </div>
    <form id="composer">
      <textarea id="prompt" rows="3" placeholder="Nachricht an cli-agent" required></textarea>
      <button id="send" type="submit">Senden</button>
      <button id="stop-run" class="secondary" type="button" hidden>Stop</button>
    </form>
  </main>
  <dialog id="sessions-dialog" class="working-context-dialog">
    <div class="dialog-header">
      <h2>Sessions</h2>
      <button id="close-sessions" class="secondary" type="button">Schließen</button>
    </div>
    <p class="meta">Jede Session kann nur in einem aktiven Browser-Tab geöffnet sein.
       Freie Sessions lassen sich erneut öffnen oder beenden.</p>
    <div id="sessions-list" class="state-list"></div>
    <button id="refresh-sessions" class="secondary" type="button">Aktualisieren</button>
  </dialog>
  <dialog id="working-context-dialog" class="working-context-dialog">
    <div class="dialog-header">
      <div>
        <h2>LLM Context</h2>
        <div class="meta">Messages des letzten Main-Loops sowie aktuell wirksame Tools und Referenzkontexte.</div>
      </div>
      <div class="dialog-actions">
        <button id="refresh-working-context" class="secondary" type="button">Aktualisieren</button>
        <button id="close-working-context" class="secondary" type="button">Schließen</button>
      </div>
    </div>
    <div class="dialog-actions"><label for="context-run-select">Agentenlauf:</label><select id="context-run-select"><option value="main">Hauptlauf</option></select></div>
    <div class="context-tabs" role="tablist" aria-label="LLM Context">
      <button id="context-tab-messages" class="secondary active" type="button" role="tab" aria-selected="true" aria-controls="context-panel-messages">Messages</button>
      <button id="context-tab-tools" class="secondary" type="button" role="tab" aria-selected="false" aria-controls="context-panel-tools">Tools</button>
      <button id="context-tab-contexts" class="secondary" type="button" role="tab" aria-selected="false" aria-controls="context-panel-contexts">Context</button>
    </div>
    <div id="context-panel-messages" class="context-panel" role="tabpanel" aria-labelledby="context-tab-messages">
      <pre id="working-messages-json">[]</pre>
    </div>
    <div id="context-panel-tools" class="context-panel hidden" role="tabpanel" aria-labelledby="context-tab-tools">
      <div id="working-tools-list" class="state-list"></div>
    </div>
    <div id="context-panel-contexts" class="context-panel hidden" role="tabpanel" aria-labelledby="context-tab-contexts">
      <div id="okf-state"></div>
      <div id="working-contexts-list" class="state-list"></div>
    </div>
  </dialog>
  <dialog id="approval">
    <h2>Tool-Freigabe erforderlich</h2>
    <div id="approval-tool" class="tool"></div>
    <div id="approval-diff-section" class="hidden"><h3>Änderungsvorschau</h3><pre id="approval-diff"></pre></div>
    <pre id="approval-arguments"></pre>
    <p id="approval-warning" class="hidden" role="alert"></p>
    <div class="approval-actions">
      <button data-decision="no" class="secondary" type="button">Nein</button>
      <button data-decision="session" class="secondary" type="button">Für Session</button>
      <button data-decision="yes" type="button">Ja</button>
    </div>
  </dialog>
</body>
</html>
"""

APP_CSS = """
:root {
  font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  color-scheme: light dark;
}
* { box-sizing: border-box; }
body { margin: 0; background: Canvas; color: CanvasText; }
.shell { max-width: 960px; min-height: 100vh; margin: 0 auto; padding: 24px; display: flex; flex-direction: column; gap: 18px; }
header { position: sticky; top: 0; z-index: 20; display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; border-bottom: 1px solid color-mix(in srgb, CanvasText 18%, transparent); padding: 14px 0; background: Canvas; }
.header-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 8px; }
h1 { margin: 0 0 4px; font-size: 1.35rem; }
.meta { opacity: .72; font-size: .9rem; overflow-wrap: anywhere; }
.chat { flex: 1; display: flex; flex-direction: column; gap: 12px; min-height: 50vh; }
.message { max-width: 88%; padding: 11px 13px; border-radius: 12px; white-space: pre-wrap; overflow-wrap: anywhere; }
.message.user { align-self: flex-end; background: color-mix(in srgb, Highlight 18%, Canvas); }
.message.assistant, .message.system { align-self: flex-start; background: color-mix(in srgb, CanvasText 8%, Canvas); }
.message.assistant { white-space: normal; overflow-x: auto; }
.message.assistant > :first-child { margin-top: 0; }
.message.assistant > :last-child { margin-bottom: 0; }
.message.assistant h1 { font-size: 1.35rem; }
.message.assistant h2 { font-size: 1.2rem; }
.message.assistant h3 { font-size: 1.08rem; }
.message.assistant h1, .message.assistant h2, .message.assistant h3 { margin: 1em 0 .45em; }
.message.assistant ul, .message.assistant ol { padding-left: 1.5rem; }
.message.assistant li + li { margin-top: .2rem; }
.message.assistant code { font-family: ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace; }
.message.assistant :not(pre) > code { padding: .1em .3em; border-radius: 4px; background: color-mix(in srgb, CanvasText 9%, Canvas); }
.message.assistant pre { max-width: 100%; overflow: auto; white-space: pre; }
.message.assistant blockquote { margin: .8em 0; padding-left: .9em; border-left: 3px solid color-mix(in srgb, CanvasText 28%, transparent); opacity: .9; }
.message.assistant table { border-collapse: collapse; display: block; max-width: 100%; overflow-x: auto; }
.message.assistant th, .message.assistant td { border: 1px solid color-mix(in srgb, CanvasText 20%, transparent); padding: 6px 9px; text-align: left; }
.message.error { align-self: flex-start; border: 1px solid #b42318; }
.message.mcp-user { align-self: center; max-width: 76%; text-align: center; background: color-mix(in srgb, Highlight 8%, Canvas); border: 1px solid color-mix(in srgb, Highlight 35%, CanvasText 15%); }
.tool-call-group { align-self: stretch; padding: 10px 12px; border-radius: 10px; border: 1px solid color-mix(in srgb, CanvasText 24%, transparent); background: color-mix(in srgb, CanvasText 4%, Canvas); font-size: .88rem; }
.tool-call-group summary { cursor: pointer; font-weight: 650; }
.tool-call-group summary:focus-visible { outline: 2px solid Highlight; outline-offset: 3px; }
.tool-call-list { margin-top: 8px; border-top: 1px solid color-mix(in srgb, CanvasText 14%, transparent); }
.tool-call-row { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; padding: 7px 0; }
.tool-call-row + .tool-call-row { border-top: 1px solid color-mix(in srgb, CanvasText 10%, transparent); }
.tool-call-name { overflow-wrap: anywhere; min-width: 0; }
.tool-call-status { flex-shrink: 0; opacity: .8; }
.tool-call-row { flex-wrap: wrap; align-items: center; }
.artifact-download { max-width: 100%; overflow-wrap: anywhere; text-align: left; padding: 6px 10px; font-size: .82rem; }
.tool-call-status[data-state="success"] { color: light-dark(#176d38, #77d89a); opacity: 1; }
.tool-call-status[data-state="error"], .tool-call-status[data-state="denied"] { color: light-dark(#b42318, #ff9088); opacity: 1; }
.commands { display: flex; flex-wrap: wrap; gap: 8px; }
.commands button { padding: 7px 10px; font-size: .9rem; }
.hidden { display: none; }
form { display: grid; grid-template-columns: 1fr auto; gap: 10px; position: sticky; bottom: 0; background: Canvas; padding: 10px 0 4px; }
textarea { width: 100%; resize: vertical; min-height: 70px; padding: 10px; font: inherit; }
button { border: 0; border-radius: 9px; padding: 10px 16px; background: Highlight; color: HighlightText; font: inherit; cursor: pointer; }
button.secondary { background: color-mix(in srgb, CanvasText 10%, Canvas); color: CanvasText; border: 1px solid color-mix(in srgb, CanvasText 20%, transparent); }
button:disabled, textarea:disabled { opacity: .55; cursor: not-allowed; }
dialog { width: min(760px, calc(100vw - 32px)); border: 1px solid color-mix(in srgb, CanvasText 20%, transparent); border-radius: 12px; padding: 20px; }
dialog::backdrop { background: rgb(0 0 0 / .45); }
.tool { font-weight: 650; margin-bottom: 10px; overflow-wrap: anywhere; }
pre { max-height: 45vh; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; padding: 12px; background: color-mix(in srgb, CanvasText 8%, Canvas); border-radius: 8px; }
.approval-actions { display: flex; justify-content: flex-end; gap: 8px; }
#approval-diff { white-space: pre; overflow-x: auto; max-height: 48vh; }
#approval-warning { color: #b42318; font-weight: 650; }
.working-context-dialog { width: min(1100px, calc(100vw - 32px)); }
.working-context-dialog pre { min-height: 55vh; max-height: 72vh; }
.state-list { display: flex; flex-direction: column; gap: 10px; }
.state-row { display: grid; grid-template-columns: minmax(170px, 1fr) minmax(260px, 2fr) auto; gap: 12px; align-items: start; padding: 12px; border: 1px solid color-mix(in srgb, CanvasText 18%, transparent); border-radius: 9px; }
.state-name { font-weight: 650; overflow-wrap: anywhere; }
.state-description { opacity: .78; white-space: pre-wrap; overflow-wrap: anywhere; }
.state-source { opacity: .7; font-size: .85rem; overflow-wrap: anywhere; }
.state-content { max-height: 16rem; overflow: auto; white-space: pre-wrap; margin-top: 6px; padding: 8px; border-radius: 6px; background: color-mix(in srgb, CanvasText 6%, Canvas); }
.state-row button { min-width: 92px; padding: 7px 10px; }
#okf-state { margin-bottom: 12px; }
.context-tabs { display: flex; gap: 8px; margin-bottom: 12px; border-bottom: 1px solid color-mix(in srgb, CanvasText 18%, transparent); padding-bottom: 8px; }
.context-tabs button.active { background: Highlight; color: HighlightText; border-color: transparent; }
.context-panel.hidden { display: none; }
.dialog-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 12px; }
.dialog-header h2 { margin: 0 0 4px; }
.dialog-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 8px; }
@media (max-width: 640px) {
  .shell { padding: 14px; }
  form { grid-template-columns: 1fr; }
  .message { max-width: 96%; }
  .state-row { grid-template-columns: 1fr; }
}
"""

def _security_headers(response: Any) -> Any:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "connect-src 'self'; "
        "img-src 'none'; "
        "object-src 'none'; "
        "frame-ancestors 'none'; "
        "base-uri 'none'; "
        "form-action 'none'"
    )
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=()"
    )
    return response


