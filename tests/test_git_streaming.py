from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from cli_agent.git_operations import GitWorkspace, GitWorkspaceError


@pytest.fixture
def child_command(monkeypatch):
    """Exercise actual pipes and cleanup with a controlled child process."""
    processes = []
    original_popen = subprocess.Popen

    def popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr("cli_agent.git_operations.subprocess.Popen", popen)

    def configure(script: str) -> None:
        monkeypatch.setattr(
            GitWorkspace, "_git_command",
            staticmethod(lambda *_: [sys.executable, "-c", script]),
        )

    yield configure
    assert processes
    assert all(process.poll() is not None for process in processes)


def test_stream_cleanup_stops_child_when_consumer_stops(child_command) -> None:
    child_command("import os,time; os.write(1, b'one\\0two\\0'); time.sleep(30)")

    with GitWorkspace._git_null_records(Path.cwd(), "unused") as records:
        assert next(records) == "one"


def test_stream_timeout_kills_child_blocked_before_output(child_command, monkeypatch) -> None:
    child_command("import time; time.sleep(30)")
    monkeypatch.setattr("cli_agent.git_operations.GIT_TIMEOUT_SECONDS", 0.25)

    with pytest.raises(GitWorkspaceError, match="Zeitlimit"):
        with GitWorkspace._git_null_records(Path.cwd(), "unused") as records:
            list(records)


def test_stream_reports_exit_error_after_output(child_command) -> None:
    child_command("import os,sys; os.write(1, b'one\\0'); os.write(2,b'stream failed'); sys.exit(3)")

    with pytest.raises(GitWorkspaceError, match="stream failed"):
        with GitWorkspace._git_null_records(Path.cwd(), "unused") as records:
            list(records)


def test_stream_rejects_incomplete_record(child_command) -> None:
    child_command("import os; os.write(1,b'missing separator')")

    with pytest.raises(GitWorkspaceError, match="unvollständig"):
        with GitWorkspace._git_null_records(Path.cwd(), "unused") as records:
            list(records)


def test_stream_rejects_oversized_record(child_command, monkeypatch) -> None:
    child_command("import os; os.write(1,b'x'*8192+b'\\0')")
    monkeypatch.setattr("cli_agent.git_operations.MAX_OUTPUT", 1024)

    with pytest.raises(GitWorkspaceError, match="Dateiname ist zu groß"):
        with GitWorkspace._git_null_records(Path.cwd(), "unused") as records:
            list(records)


def test_stream_keeps_unicode_and_newlines_across_read_boundaries(child_command) -> None:
    filename = "x" * 65535 + "€\nend"
    child_command("import os; os.write(1, b'x'*65535+'€\\nend\\0'.encode('utf-8'))")

    with GitWorkspace._git_null_records(Path.cwd(), "unused") as records:
        assert list(records) == [filename]


def test_stream_does_not_block_on_stderr(child_command) -> None:
    child_command("import os; os.write(2,b'warning'*30000); os.write(1,b'one\\0')")

    with GitWorkspace._git_null_records(Path.cwd(), "unused") as records:
        assert list(records) == ["one"]
