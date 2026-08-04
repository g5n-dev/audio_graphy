"""Report which optional optimizer extras this process can use.

Detection reads distribution metadata and never imports the package. That is not a
micro-optimisation: importing ``dspy`` pulls in litellm, which emits
``DeprecationWarning`` at import time, and the test suite runs under
``filterwarnings = ["error"]``. A probe implemented as ``try: import dspy`` would
therefore turn "is DSPy available?" into a test failure on every machine where it
*is* installed -- the exact opposite of what the probe is for.

The API image never installs these extras. Only the optimizer worker does, which is
why the answer has to be a runtime fact rather than a build-time assumption.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import metadata

#: Distribution names, not module names. ``dspy`` installs the ``dspy`` module, but
#: the two are allowed to differ and only the distribution name is what
#: :func:`importlib.metadata.version` understands.
DSPY_DISTRIBUTION = "dspy"
TEXTGRAD_DISTRIBUTION = "textgrad"


class MissingExtraError(RuntimeError):
    """Raised when a compiler needs an extra this process does not have."""


@dataclass(frozen=True, slots=True)
class ExtraStatus:
    """Whether one optional distribution is installed, and at which version."""

    distribution: str
    version: str | None

    @property
    def installed(self) -> bool:
        return self.version is not None

    def require(self) -> str:
        """Return the installed version, or explain how to get it."""

        if self.version is None:
            raise MissingExtraError(
                f"需要可选依赖 {self.distribution}，当前进程未安装。"
                f'optimizer worker 镜像应以 pip install -e ".[optimizer]" 构建。'
            )
        return self.version


def probe(distribution: str) -> ExtraStatus:
    """Look up *distribution* in the installed metadata without importing it."""

    try:
        version = metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return ExtraStatus(distribution=distribution, version=None)
    return ExtraStatus(distribution=distribution, version=version)


def dspy_status() -> ExtraStatus:
    return probe(DSPY_DISTRIBUTION)


def textgrad_status() -> ExtraStatus:
    return probe(TEXTGRAD_DISTRIBUTION)
