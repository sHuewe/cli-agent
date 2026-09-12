from __future__ import annotations

import re
import subprocess  # nosec B404
from collections.abc import Callable
from dataclasses import dataclass


class PythonValidationError(RuntimeError):
    """The Python validation request is invalid."""


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
_MODULE_NAME = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$")
_IMAGE_DIGEST = re.compile(r"^.+@sha256:[0-9a-f]{64}$", re.IGNORECASE)


def is_pinned_image(image: str) -> bool:
    """Return whether a Docker image reference is immutable by digest."""
    return bool(_IMAGE_DIGEST.fullmatch(image.strip()))


@dataclass(frozen=True)
class ValidatorSettings:
    python_image: str = ""
    network_mode: str = "none"
    setup_timeout_seconds: int = 180
    startup_grace_seconds: float = 3.0
    memory_limit: str = "2g"
    cpu_limit: str = "2.0"
    pids_limit: int = 256
    log_lines: int = 200
    require_pinned_image: bool = True

    def __post_init__(self) -> None:
        if not self.python_image.strip():
            raise ValueError("python_image darf nicht leer sein.")
        if self.require_pinned_image and not is_pinned_image(self.python_image):
            raise ValueError(
                "Für das abgesicherte Validator-Profil muss python_image einen "
                "vollständigen sha256-Digest enthalten."
            )
        if self.network_mode not in {"none", "bridge"}:
            raise ValueError("network_mode muss 'none' oder 'bridge' sein.")
