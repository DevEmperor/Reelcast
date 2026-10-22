"""Gemeinsame Datentypen: Folge, Optionen, Fortschrittsmeldungen, Abbruch."""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from pathlib import Path


class Cancelled(Exception):
    """Der Nutzer hat abgebrochen."""


class ReelcastError(Exception):
    """Fehler mit einer für Menschen gedachten Meldung."""


@dataclass
class Episode:
    id: str
    title: str
    show: str = ""
    duration: float = 0.0  # Sekunden, 0 = unbekannt
    release: str = ""  # JJJJ-MM-TT
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
    mode: str = "auto"  # auto | feed | spotify
    fmt: str = "mp3"  # mp3 | m4a | opus | flac
    quality: int = 2  # LAME-VBR-Stufe 0 (beste) … 9
    listen: bool = False  # Aufnahme über die Lautsprecher mithören
    overwrite: bool = False
    force: bool = False  # laufende Spotify-Wiedergabe unterbrechen
    show_spotify: bool = False  # selbst gestartetes Spotify nicht minimieren
    folder_per_show: bool = True


@dataclass
class Result:
    path: Path
    source: str  # "feed" | "spotify"
    duration: float
    skipped: bool = False
    note: str = ""


class Reporter:
    """Rückmeldungen aus dem Ablauf. CLI und GUI überschreiben, was sie brauchen.

    Die Methoden werden aus dem Arbeitsthread aufgerufen.
    """

    def episode(self, ep: Episode) -> None: ...

    def phase(self, text: str) -> None: ...

    def info(self, text: str) -> None: ...

    def warn(self, text: str) -> None: ...

    def progress(self, done: float, total: float | None, unit: str = "s") -> None:
        """unit "s" = Audiosekunden, "B" = Bytes."""

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
        """Schlafen, aber sofort bei Abbruch aufwachen."""
        if self.event.wait(seconds):
            raise Cancelled()


_UNSAFE = re.compile(r'[\\:*?"<>|\x00-\x1f]')


def safe_filename(text: str, limit: int = 180) -> str:
    """Dateiname aus einem Folgentitel; "/" wird zum Gedankenstrich."""
    text = text.replace("/", " - ")
    text = text.replace("„", "").replace("“", "").replace("”", "").replace('"', "")
    text = _UNSAFE.sub("", text)
    text = re.sub(r"\s*-\s*(-\s*)+", " - ", text)
    text = re.sub(r"\s+", " ", text).strip(" .-")
    if len(text) > limit:
        text = text[:limit].rstrip(" .-")
    return text or "Folge"


def fmt_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
