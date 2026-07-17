"""Colored, timestamped console logging helpers with no external dependencies."""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from typing import TextIO

_RESET = "\033[0m"
_BOLD = "\033[1m"

_COLORS = {
    "blue": "\033[34m",
    "cyan": "\033[36m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "magenta": "\033[35m",
    "red": "\033[31m",
    "grey": "\033[90m",
}


def _color_enabled(stream: TextIO) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return stream.isatty()


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%H:%M:%S")


def _emit(message: str, color: str, icon: str, stream: TextIO) -> None:
    line = f"{icon} [{_timestamp()}] {message}"
    if _color_enabled(stream):
        line = f"{_COLORS[color]}{line}{_RESET}"
    stream.write(line + "\n")


def step(message: str) -> None:
    _emit(message, "magenta", "==>", sys.stdout)


def sub(message: str) -> None:
    _emit(message, "cyan", "  -", sys.stdout)


def info(message: str) -> None:
    _emit(message, "blue", "  i", sys.stdout)


def ok(message: str) -> None:
    _emit(message, "green", "  +", sys.stdout)


def warn(message: str) -> None:
    _emit(message, "yellow", "  !", sys.stdout)


def error(message: str) -> None:
    _emit(message, "red", "  x", sys.stderr)


def debug(message: str) -> None:
    if os.environ.get("BOOTSTRAP_DEBUG"):
        _emit(message, "grey", "  .", sys.stdout)


def banner(title: str) -> None:
    text = f"====[ {title} ]===="
    if _color_enabled(sys.stdout):
        text = f"{_COLORS['magenta']}{_BOLD}{text}{_RESET}"
    sys.stdout.write(text + "\n")
