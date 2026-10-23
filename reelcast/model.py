"""Shared types: episode, options, progress reporting, cancellation."""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

# Phases of a single-episode recording. Front ends compare against these constants,
# never against display text.
PHASE_RESOLVE = "Reading episode details…"
PHASE_START_SPOTIFY = "Starting Spotify…"
PHASE_CONNECT = "Connecting to Spotify…"
PHASE_OPEN = "Opening the episode in Spotify…"
PHASE_RECORDING = "Recording…"
PHASE_ENCODING = "Encoding…"


class Cancelled(Exception):
    """The user cancelled."""


class ReelcastError(Exception):
    """Error with a message meant for people."""


@dataclass
class Episode:
    id: str
    title: str
    show: str = ""
    duration: float = 0.0  # seconds, 0 = unknown
    release: str = ""  # YYYY-MM-DD
    cover_url: str = ""
    playable: bool = True

    @property
    def uri(self) -> str:
        return f"spotify:episode:{self.id}"

    @property
    def url(self) -> str:
        return f"https://open.spotify.com/episode/{self.id}"


@dataclass
class Options:
    out_dir: Path
    fmt: str = "mp3"  # mp3 | m4a | opus | flac
    quality: int = 2  # LAME VBR level, 0 (best) … 9
    listen: bool = False  # hear the recording through the speakers
    overwrite: bool = False
    force: bool = False  # interrupt whatever Spotify is playing
    show_spotify: bool = False  # don't minimize a Spotify we started ourselves


@dataclass
class Result:
    path: Path
    source: str  # "spotify" | "existing"
    duration: float
    skipped: bool = False
    note: str = ""


class Reporter:
    """Feedback from the work thread. CLI and GUI override what they need.

    The methods are called from the work thread.
    """

    def episode(self, ep: Episode) -> None: ...

    def phase(self, text: str) -> None: ...

    def info(self, text: str) -> None: ...

    def warn(self, text: str) -> None: ...

    def progress(self, done: float, total: float | None, unit: str = "s") -> None:
        """unit "s" = seconds of audio, "B" = bytes."""

    def level(self, peak_db: float) -> None: ...


@dataclass
class CancelToken:
    event: threading.Event = field(default_factory=threading.Event)

    def cancel(self) -> None:
        self.event.set()

    @property
    def cancelled(self) -> bool:
        return self.event.is_set()

    def check(self) -> None:
        if self.event.is_set():
            raise Cancelled()

    def sleep(self, seconds: float) -> None:
        """Sleep, but wake up immediately on cancel."""
        if self.event.wait(seconds):
            raise Cancelled()


_UNSAFE = re.compile(r'[\\:*?"<>|\x00-\x1f]')


def safe_filename(text: str, limit: int = 180) -> str:
    """File name from a title; "/" becomes a dash."""
    text = text.replace("/", " - ")
    text = text.replace("„", "").replace("“", "").replace("”", "").replace('"', "")
    text = _UNSAFE.sub("", text)
    text = re.sub(r"\s*-\s*(-\s*)+", " - ", text)
    text = re.sub(r"\s+", " ", text).strip(" .-")
    if len(text) > limit:
        text = text[:limit].rstrip(" .-")
    return text or "Untitled"


def fmt_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
