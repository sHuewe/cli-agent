"""Regression tests for the canonical package layout and isolated workers."""

from __future__ import annotations

import importlib
from pathlib import Path
import subprocess
import sys

import pytest


IMPLEMENTATIONS = {
    "data": (
        "data_calculator", "data_markdown", "data_mcp_server",
        "data_operations", "data_result_metadata", "dataframe_operations",
        "dataframe_types",
    ),
    "models": (
        "model", "model_factory", "model_http", "ollama",
        "openai_client", "prompt_template",
    ),
    "context": (
        "file_context", "web_context", "web_context_agent",
        "pdf_text", "knowledge_vector", "agent_knowledge",
        "agent_knowledge_support", "knowledge_cli", "rag_cli",
    ),
    "security": (
        "filesystem_security", "network_policy", "mcp_path_policy",
        "mcp_schema_guard", "mcp_limits", "mcp_contracts",
        "mcp_result_artifacts",
    ),
    "state": (
        "evidence_state", "evidence_tools", "sub_runs",
        "agent_permissions", "agent_types", "message_visibility",
    ),
    "workflows": (
        "flow", "flow_filesystem_operations", "flow_filesystem_windows",
        "flow_json_grouping", "flow_json_operations", "flow_json_validation",
    ),
    "runtime": (
        "agent", "agent_conversation", "agent_loop", "agent_mcp",
        "agent_tool_calls", "agent_prompts",
    ),
    "interfaces": (
        "agent_api", "cli", "web_ui", "web_ui_api", "web_ui_sessions",
        "ag_ui_protocol", "ag_ui_frontend_tools",
        "approval_display", "terminal_output",
    ),
    "platform": (
        "os_mcp_server", "os_operations", "execution", "local_commands",
        "logging_setup", "workspace_access", "artifact_publication",
    ),
    "settings": ("config", "admin_config"),
}


@pytest.mark.parametrize(
    ("package", "module"),
    [(package, module)
     for package, modules in IMPLEMENTATIONS.items()
     for module in modules],
)
def test_canonical_package_imports_without_root_aliases(package, module):
    module_name = f"cli_agent.{package}.{module}"
    implementation = importlib.import_module(module_name)
    assert implementation.__name__ == module_name
    assert implementation.__file__ is not None
    assert Path(implementation.__file__).parent.name == package


def test_package_root_contains_no_legacy_shims():
    from cli_agent import __file__ as package_file

    root = Path(package_file).resolve().parent
    assert {entry.name for entry in root.glob("*.py")} == {"__init__.py"}


def test_pdf_worker_invocable_by_canonical_name():
    result = subprocess.run(
        [sys.executable, "-m", "cli_agent.context.pdf_text"],
        input=b"not a PDF",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=15,
    )
    assert result.returncode == 2
    assert b"PDF" in result.stdout


def test_schema_worker_handshake_invocable_by_canonical_name():
    result = subprocess.run(
        [sys.executable, "-m", "cli_agent.security.mcp_schema_guard"],
        input=b"",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=15,
    )
    assert result.returncode == 0
    assert result.stdout.splitlines() == [b'{"ready":true}']
