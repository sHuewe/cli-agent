"""Package migration contracts: preserve legacy module identity and workers."""

from __future__ import annotations

import importlib
import subprocess
import sys

import pytest

MOVED_MODULES = {
    "data": (
        "data_calculator", "data_markdown", "data_operations",
        "data_result_metadata", "dataframe_operations", "dataframe_types",
    ),
    "models": (
        "model", "model_factory", "model_http", "ollama",
        "openai_client", "prompt_template",
    ),
    "context": (
        "file_context", "web_context", "web_context_agent",
        "pdf_text", "knowledge_vector", "agent_knowledge",
        "agent_knowledge_support",
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
        "flow_filesystem_operations", "flow_filesystem_windows",
        "flow_json_grouping", "flow_json_operations", "flow_json_validation",
    ),
}


@pytest.mark.parametrize(
    ("package", "module"),
    [(package, module) for package, modules in MOVED_MODULES.items()
     for module in modules],
)
def test_legacy_module_and_new_package_share_identity(package, module):
    original = importlib.import_module(f"cli_agent.{module}")
    migrated = importlib.import_module(f"cli_agent.{package}.{module}")
    assert original is migrated


def test_pdf_worker_remains_invokable_via_legacy_module_name():
    result = subprocess.run(
        [sys.executable, "-m", "cli_agent.pdf_text"],
        input=b"not a PDF",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=15,
    )
    assert result.returncode == 2
    assert b"PDF" in result.stdout


def test_schema_worker_handshake_remains_available_via_legacy_module_name():
    result = subprocess.run(
        [sys.executable, "-m", "cli_agent.mcp_schema_guard"],
        input=b"",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=15,
    )
    assert result.returncode == 0
    assert result.stdout.splitlines() == [b'{"ready":true}']
