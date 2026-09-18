from __future__ import annotations

from pathlib import Path

import pytest

import cli_agent.file_context as file_context_module
from cli_agent.file_context import (
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
        context_file=Path("repository.txt"),
        prompt_file=None,
        output=None,
        overwrite_output=False,
    )

    assert prepared is not None
    assert prepared.relative_path == "repository.txt"
    assert prepared.content == "complete repository"
    assert prompt is None
    assert output is None


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
        context_file=None,
        prompt_file=None,
        output=Path("review.md"),
        overwrite_output=False,
    )

    assert context is None
    assert prompt is None
    assert output is not None
    output.write_text("review")
    assert (tmp_path / "review.md").read_text(encoding="utf-8") == "review"


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
            context_file=Path("repository.txt"),
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
            context_file=Path("input.txt"),
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
