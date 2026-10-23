"""Locate external programs: ffmpeg, ffprobe and (on macOS) the Reelcast helper.

Order: environment variable, the app bundle's own copy, then PATH.
"""

from __future__ import annotations

import os
import shutil
import sys
from functools import lru_cache
from pathlib import Path

IS_MAC = sys.platform == "darwin"


def _bundled_dirs() -> list[Path]:
    dirs = []
    if getattr(sys, "_MEIPASS", None):  # PyInstaller
        dirs.append(Path(sys._MEIPASS) / "bin")
    here = Path(__file__).resolve().parent
    dirs += [here / "bin", here.parent / "macos" / "build"]
    return dirs


@lru_cache(maxsize=None)
def find(name: str) -> str:
    env = os.environ.get(f"REELCAST_{name.upper().replace('-', '_')}")
    if env:
        return env
    for d in _bundled_dirs():
        candidate = d / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return shutil.which(name) or name


def ffmpeg() -> str:
    return find("ffmpeg")


def ffprobe() -> str:
    return find("ffprobe")


def helper() -> str:
    return find("reelcast-helper")
