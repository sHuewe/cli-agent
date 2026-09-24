from __future__ import annotations

from pathlib import Path

import pytest

import cli_agent.file_context as file_context_module
from cli_agent.file_context import (
    FILE_CONTEXT_SYSTEM_RULE,
    ContextFileCliAgent,
    FileContext,
    OutputTarget,
    prepare_context_file,
    prepare_file_options,
    prepare_output_target,
    prepare_prompt_file,
)


def test_context_file_does_not_require_os_access(tmp_path: Path) -> None:
    context = tmp_path / "repository.txt"
    context.write_text("complete repository", encoding="utf-8")

    prepared, prompt, output = prepare_file_options(
        tmp_path,
        context_files=(Path("repository.txt"),),
        prompt_file=None,
        output=None,
        overwrite_output=False,
    )

    assert len(prepared) == 1
    assert prepared[0].relative_path == "repository.txt"
    assert prepared[0].content == "complete repository"
    assert prompt is None
    assert output is None



def test_multiple_context_files_are_prepared_in_order(tmp_path: Path) -> None:
    (tmp_path / "one.txt").write_text("one", encoding="utf-8")
    (tmp_path / "two.txt").write_text("two", encoding="utf-8")

    contexts, prompt, output = prepare_file_options(
        tmp_path,
        context_files=(Path("one.txt"), Path("two.txt")),
        prompt_file=None,
        output=None,
        overwrite_output=False,
    )

    assert [context.relative_path for context in contexts] == ["one.txt", "two.txt"]
    assert [context.content for context in contexts] == ["one", "two"]
    assert prompt is None
    assert output is None


def test_multiple_context_files_reject_duplicate_resolved_path(tmp_path: Path) -> None:
    (tmp_path / "one.txt").write_text("one", encoding="utf-8")

    with pytest.raises(ValueError, match="nicht mehrfach"):
        prepare_file_options(
            tmp_path,
            context_files=(Path("one.txt"), Path("./one.txt")),
            prompt_file=None,
            output=None,
            overwrite_output=False,
        )


def test_multiple_context_files_have_aggregate_size_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(file_context_module, "MAX_LLM_CONTEXT_TOTAL_BYTES", 6)
    (tmp_path / "one.txt").write_text("1234", encoding="utf-8")
    (tmp_path / "two.txt").write_text("5678", encoding="utf-8")

    with pytest.raises(ValueError, match="zusammen das Sicherheitslimit"):
        prepare_file_options(
            tmp_path,
            context_files=(Path("one.txt"), Path("two.txt")),
            prompt_file=None,
            output=None,
            overwrite_output=False,
        )



def test_context_agent_exposes_multiple_files_as_reference_payload(
    tmp_path: Path,
) -> None:
    contexts = (
        FileContext("one.txt", "one", tmp_path / "one.txt", 3),
        FileContext("two.txt", "two", tmp_path / "two.txt", 3),
    )
    agent = ContextFileCliAgent(
        tmp_path,
        object(),
        (),
        file_contexts=contexts,
    )

    payload = agent._reference_context_payload(knowledge="known")

    assert payload["retrieved_okf_knowledge"] == "known"
    assert payload["local_reference_files"] == [
        {"workspace_path": "one.txt", "content": "one"},
        {"workspace_path": "two.txt", "content": "two"},
    ]
    assert FILE_CONTEXT_SYSTEM_RULE.strip() in agent._build_system_prompt()


def test_context_agent_keeps_single_file_payload_compatible(
    tmp_path: Path,
) -> None:
    context = FileContext("one.txt", "one", tmp_path / "one.txt", 3)
    agent = ContextFileCliAgent(
        tmp_path,
        object(),
        (),
        file_context=context,
    )

    payload = agent._reference_context_payload(knowledge=None)

    assert payload["local_reference_file"] == {
        "workspace_path": "one.txt",
        "content": "one",
    }
    assert "local_reference_files" not in payload


def test_context_agent_rejects_legacy_and_multi_context_arguments_together(
    tmp_path: Path,
) -> None:
    context = FileContext("one.txt", "one", tmp_path / "one.txt", 3)

    with pytest.raises(ValueError, match="nicht gleichzeitig"):
        ContextFileCliAgent(
            tmp_path,
            object(),
            (),
            file_context=context,
            file_contexts=(context,),
        )


def test_context_agent_without_files_does_not_add_file_rule(
    tmp_path: Path,
) -> None:
    agent = ContextFileCliAgent(tmp_path, object(), ())

    assert agent._reference_context_payload(knowledge=None) == {}
    assert FILE_CONTEXT_SYSTEM_RULE.strip() not in agent._build_system_prompt()

def test_context_file_accepts_absolute_path_inside_workspace(tmp_path: Path) -> None:
    context = tmp_path / "review" / "repository.txt"
    context.parent.mkdir()
    context.write_text("repo", encoding="utf-8")

    prepared = prepare_context_file(tmp_path, context.resolve())

    assert prepared.relative_path == "review/repository.txt"


def test_context_file_rejects_path_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    with pytest.raises(ValueError, match="innerhalb des Workspace"):
        prepare_context_file(workspace, outside.resolve())


def test_context_file_rejects_sensitive_workspace_file(tmp_path: Path) -> None:
    context = tmp_path / ".env"
    context.write_text("API_KEY=secret", encoding="utf-8")

    with pytest.raises(ValueError, match="Secret-/Credential"):
        prepare_context_file(tmp_path, Path(".env"))


def test_context_file_rejects_parent_reference_even_if_it_resolves_inside(
    tmp_path: Path,
) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    context = tmp_path / "repository.txt"
    context.write_text("repo", encoding="utf-8")

    with pytest.raises(ValueError, match="'..'"):
        prepare_context_file(tmp_path, Path("nested/../repository.txt"))


def test_context_file_rejects_symlink_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = workspace / "repository.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Symlinks are not available in this test environment")

    with pytest.raises(ValueError, match="innerhalb des Workspace"):
        prepare_context_file(workspace, Path("repository.txt"))


def test_context_file_limit_is_generous_but_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(file_context_module, "MAX_LLM_INPUT_FILE_BYTES", 16)
    context = tmp_path / "repository.txt"
    context.write_bytes(b"x" * 17)

    with pytest.raises(ValueError, match="Sicherheitslimit"):
        prepare_context_file(tmp_path, Path("repository.txt"))


def test_context_file_at_size_limit_is_accepted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(file_context_module, "MAX_LLM_INPUT_FILE_BYTES", 16)
    context = tmp_path / "repository.txt"
    context.write_bytes(b"x" * 16)

    prepared = prepare_context_file(tmp_path, Path("repository.txt"))

    assert prepared.content == "x" * 16


def test_prompt_file_is_loaded_as_workspace_text(tmp_path: Path) -> None:
    prompt = tmp_path / "review-prompt.md"
    prompt.write_text("Review the repository thoroughly.\n", encoding="utf-8")

    prepared = prepare_prompt_file(tmp_path, Path("review-prompt.md"))

    assert prepared.relative_path == "review-prompt.md"
    assert prepared.content == "Review the repository thoroughly.\n"


def test_prompt_file_uses_same_bounded_reader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(file_context_module, "MAX_LLM_INPUT_FILE_BYTES", 8)
    prompt = tmp_path / "prompt.md"
    prompt.write_bytes(b"x" * 9)

    with pytest.raises(ValueError, match="Sicherheitslimit"):
        prepare_prompt_file(tmp_path, Path("prompt.md"))


def test_prompt_file_rejects_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "prompt.md"
    outside.write_text("review", encoding="utf-8")

    with pytest.raises(ValueError, match="innerhalb des Workspace"):
        prepare_prompt_file(workspace, outside.resolve())


def test_prompt_file_rejects_sensitive_file(tmp_path: Path) -> None:
    prompt = tmp_path / ".env"
    prompt.write_text("secret", encoding="utf-8")

    with pytest.raises(ValueError, match="Secret-/Credential"):
        prepare_prompt_file(tmp_path, Path(".env"))


def test_prompt_file_rejects_empty_file(tmp_path: Path) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text(" \n\t", encoding="utf-8")

    with pytest.raises(ValueError, match="darf nicht leer sein"):
        prepare_prompt_file(tmp_path, Path("prompt.md"))


def test_output_does_not_require_os_write(tmp_path: Path) -> None:
    context, prompt, output = prepare_file_options(
        tmp_path,
        context_files=(),
        prompt_file=None,
        output=Path("review.md"),
        overwrite_output=False,
    )

    assert context == ()
    assert prompt is None
    assert output is not None
    output.write_text("review")
    assert (tmp_path / "review.md").read_text(encoding="utf-8") == "review"


@pytest.mark.parametrize(
    "path",
    [
        Path(".env"),
        Path(".env.local"),
        Path(".git") / "config",
        Path(".cli-agent") / "context.json",
        Path("secret.pem"),
        Path("app.log"),
    ],
)
def test_output_rejects_sensitive_workspace_path(
    tmp_path: Path,
    path: Path,
) -> None:
    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)

    with pytest.raises(ValueError, match="Secret-/Credential- oder interner"):
        prepare_output_target(tmp_path, path, overwrite=False)


def test_output_rejects_sensitive_existing_file_even_with_overwrite(
    tmp_path: Path,
) -> None:
    output = tmp_path / ".env"
    output.write_text("OLD=value", encoding="utf-8")

    with pytest.raises(ValueError, match="Secret-/Credential- oder interner"):
        prepare_output_target(tmp_path, Path(".env"), overwrite=True)

    assert output.read_text(encoding="utf-8") == "OLD=value"


def test_existing_output_requires_explicit_overwrite(tmp_path: Path) -> None:
    output = tmp_path / "review.md"
    output.write_text("old", encoding="utf-8")

    with pytest.raises(ValueError, match="--overwrite-output"):
        prepare_output_target(tmp_path, Path("review.md"), overwrite=False)

    target = prepare_output_target(tmp_path, Path("review.md"), overwrite=True)
    target.write_text("new")
    assert output.read_text(encoding="utf-8") == "new"


def test_overwrite_output_requires_output(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="nur zusammen mit --output"):
        prepare_file_options(
            tmp_path,
            context_file=None,
            prompt_file=None,
            output=None,
            overwrite_output=True,
        )


def test_context_and_output_must_not_be_same_file(tmp_path: Path) -> None:
    path = tmp_path / "repository.txt"
    path.write_text("repo", encoding="utf-8")

    with pytest.raises(ValueError, match="dieselbe Datei"):
        prepare_file_options(
            tmp_path,
            context_files=(Path("repository.txt"),),
            prompt_file=None,
            output=Path("repository.txt"),
            overwrite_output=True,
        )


def test_prompt_and_output_must_not_be_same_file(tmp_path: Path) -> None:
    path = tmp_path / "review.md"
    path.write_text("review prompt", encoding="utf-8")

    with pytest.raises(ValueError, match="dieselbe Datei"):
        prepare_file_options(
            tmp_path,
            context_file=None,
            prompt_file=Path("review.md"),
            output=Path("review.md"),
            overwrite_output=True,
        )


def test_context_and_prompt_must_not_be_same_file(tmp_path: Path) -> None:
    path = tmp_path / "input.txt"
    path.write_text("input", encoding="utf-8")

    with pytest.raises(ValueError, match="dieselbe Datei"):
        prepare_file_options(
            tmp_path,
            context_files=(Path("input.txt"),),
            prompt_file=Path("input.txt"),
            output=None,
            overwrite_output=False,
        )


def test_output_rejects_path_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(ValueError, match="innerhalb des Workspace"):
        prepare_output_target(
            workspace,
            (tmp_path / "review.md").resolve(),
            overwrite=False,
        )


def test_output_target_updates_same_new_file_during_session(tmp_path: Path) -> None:
    target: OutputTarget = prepare_output_target(
        tmp_path,
        Path("review.md"),
        overwrite=False,
    )

    target.write_text("first")
    target.write_text("second")

    assert (tmp_path / "review.md").read_text(encoding="utf-8") == "second"


def test_aggregate_context_limit_stops_before_loading_later_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(file_context_module, "MAX_LLM_CONTEXT_TOTAL_BYTES", 6)
    (tmp_path / "one.txt").write_text("1234", encoding="utf-8")
    (tmp_path / "two.txt").write_text("5678", encoding="utf-8")
    (tmp_path / "three.txt").write_text("later", encoding="utf-8")

    original = file_context_module.prepare_context_file
    loaded = []

    def recording_prepare(workspace: Path, path: Path, **kwargs):
        loaded.append(path)
        return original(workspace, path, **kwargs)

    monkeypatch.setattr(
        file_context_module,
        "prepare_context_file",
        recording_prepare,
    )

    with pytest.raises(ValueError, match="zusammen das Sicherheitslimit"):
        prepare_file_options(
            tmp_path,
            context_files=(
                Path("one.txt"),
                Path("two.txt"),
                Path("three.txt"),
            ),
            prompt_file=None,
            output=None,
            overwrite_output=False,
        )

    assert loaded == [Path("one.txt"), Path("two.txt")]


@pytest.mark.parametrize("ancestor_name", ["secrets", "credentials", ".git", ".ssh"])
def test_direct_file_options_ignore_sensitive_workspace_ancestor_name(
    tmp_path: Path,
    ancestor_name: str,
) -> None:
    workspace = tmp_path / ancestor_name / "project"
    workspace.mkdir(parents=True)
    (workspace / "context.txt").write_text("context", encoding="utf-8")
    (workspace / "prompt.md").write_text("prompt", encoding="utf-8")

    context = prepare_context_file(workspace, Path("context.txt"))
    prompt = prepare_prompt_file(workspace, Path("prompt.md"))
    output = prepare_output_target(workspace, Path("output.md"), overwrite=False)

    assert context.content == "context"
    assert prompt.content == "prompt"
    output.write_text("result")
    assert (workspace / "output.md").read_text(encoding="utf-8") == "result"
