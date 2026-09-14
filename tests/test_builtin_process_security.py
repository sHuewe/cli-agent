from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from cli_agent.agent import CliAgent


def test_stdio_environment_enables_python_safe_path_for_all_servers(monkeypatch) -> None:
    monkeypatch.setenv("PYTHONPATH", "attacker-controlled")

    for built_in in (True, False):
        environment = CliAgent._stdio_environment(built_in=built_in)

        assert environment["PYTHONSAFEPATH"] == "1"
        assert "PYTHONPATH" not in environment


def test_stdio_python_child_keeps_workspace_cwd_without_importing_from_it(tmp_path: Path) -> None:
    (tmp_path / "workspace_shadow_probe.py").write_text("VALUE = 'workspace'\n", encoding="utf-8")

    for built_in in (True, False):
        environment = CliAgent._stdio_environment(built_in=built_in)
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import importlib.util, os; "
                    "print(os.getcwd()); "
                    "print(importlib.util.find_spec('workspace_shadow_probe'))"
                ),
            ],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        )

        output = completed.stdout.splitlines()
        assert Path(output[0]).resolve() == tmp_path.resolve()
        assert output[1].strip() == "None"
