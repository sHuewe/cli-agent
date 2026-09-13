from __future__ import annotations

from pathlib import Path

import pytest

from cli_agent.file_context import (
    OutputTarget,
    prepare_context_file,
    prepare_file_options,
    prepare_output_target,
)


def test_context_file_requires_explicit_os_access(tmp_path: Path) -> None:
    context = tmp_path / "repository.txt"
    context.write_text("repo", encoding="utf-8")

    with pytest.raises(ValueError, match="--with-os-read"):
        prepare_file_options(
            tmp_path,
            context_file=Path("repository.txt"),
            output=None,
            overwrite_output=False,
            os_access=None,
        )


@pytest.mark.parametrize("access", ["read", "write"])
def test_context_file_is_allowed_with_explicit_os_access(
    tmp_path: Path,
    access: str,
) -> None:
    context = tmp_path / "repository.txt"
    context.write_text("complete repository", encoding="utf-8")

    prepared, output = prepare_file_options(
        tmp_path,
        context_file=Path("repository.txt"),
        output=None,
        overwrite_output=False,
        os_access=access,
    )

    assert prepared is not None
    assert prepared.relative_path == "repository.txt"
    assert prepared.content == "complete repository"
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


def test_output_does_not_require_os_write(tmp_path: Path) -> None:
    context, output = prepare_file_options(
        tmp_path,
        context_file=None,
        output=Path("review.md"),
        overwrite_output=False,
        os_access=None,
    )

    assert context is None
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
            output=None,
            overwrite_output=True,
            os_access=None,
        )


def test_context_and_output_must_not_be_same_file(tmp_path: Path) -> None:
    path = tmp_path / "repository.txt"
    path.write_text("repo", encoding="utf-8")

    with pytest.raises(ValueError, match="dieselbe Datei"):
        prepare_file_options(
            tmp_path,
            context_file=Path("repository.txt"),
            output=Path("repository.txt"),
            overwrite_output=True,
            os_access="read",
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
