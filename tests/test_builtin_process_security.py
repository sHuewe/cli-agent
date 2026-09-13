from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from cli_agent.agent import CliAgent


def test_builtin_stdio_environment_enables_python_safe_path(monkeypatch) -> None:
    monkeypatch.setenv("PYTHONPATH", "attacker-controlled")

    environment = CliAgent._stdio_environment(built_in=True)

    assert environment["PYTHONSAFEPATH"] == "1"
    assert "PYTHONPATH" not in environment


def test_builtin_python_child_does_not_import_module_from_workspace(tmp_path: Path) -> None:
    (tmp_path / "workspace_shadow_probe.py").write_text("VALUE = 'workspace'\n", encoding="utf-8")
    environment = CliAgent._stdio_environment(built_in=True)

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import importlib.util; "
                "print(importlib.util.find_spec('workspace_shadow_probe'))"
            ),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )

    assert completed.stdout.strip() == "None"


def test_untrusted_stdio_environment_is_not_silently_reclassified() -> None:
    environment = CliAgent._stdio_environment(built_in=False)

    assert environment.get("PYTHONSAFEPATH") is None
