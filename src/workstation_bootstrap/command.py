"""Thin subprocess wrapper with consistent logging and error handling."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence

from . import console


class CommandError(RuntimeError):
    """Raised when a command exits with a non-zero status."""


def run(
    cmd: Sequence[str],
    *,
    check: bool = True,
    capture: bool = False,
    input_text: str | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command, logging it and raising CommandError on failure when check is True."""
    console.debug("run: " + " ".join(cmd))
    result = subprocess.run(
        list(cmd),
        check=False,
        text=True,
        capture_output=capture,
        input=input_text,
        env=dict(env) if env is not None else None,
    )
    if check and result.returncode != 0:
        stderr = (result.stderr or "").strip()
        detail = f"\n{stderr}" if stderr else ""
        raise CommandError(f"command failed ({result.returncode}): {' '.join(cmd)}{detail}")
    return result
