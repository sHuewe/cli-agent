"""Safe, explicit snapshots of artifacts published by the agent."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# Publishing does not make arbitrary files visible to the SDK: the runtime
# verifies ownership through McpResultArtifacts.read_registered before we
# snapshot any bytes.
MAX_SDK_ARTIFACTS_PER_RUN = 32
MAX_SDK_ARTIFACT_BYTES_PER_RUN = 64 * 1024 * 1024


@dataclass
class AgentArtifact:
    """Verified in-memory snapshot, independent of the MCP cleanup lifecycle.

    A local path exists only after the Python caller explicitly saves it.
    No directories or files are created implicitly.
    """

    name: str
    mime_type: str
    _content: bytes = field(repr=False)
    path: Path | None = None

    @property
    def size(self) -> int:
        return len(self._content)

    def read_bytes(self) -> bytes:
        return self._content

    def save(self, destination: str | Path) -> Path:
        """Write once, without silently overwriting an existing user file.

        A directory destination uses the published name; a file destination
        uses the explicit filename chosen by the Python caller.
        """
        target = Path(destination).expanduser()
        if target.is_dir():
            target = target / self.name
        with target.open("xb") as stream:
            stream.write(self._content)
        self.path = target
        return target
