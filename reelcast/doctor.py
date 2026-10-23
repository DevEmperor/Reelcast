"""System check: is everything there, and which Spotify settings affect recording?"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .player import spotify_command

OK, WARN, FAIL = "ok", "warn", "fail"


def _out(*cmd: str) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def spotify_prefs() -> dict[str, str]:
    prefs: dict[str, str] = {}
    for base in (
        Path.home() / ".var/app/com.spotify.Client/config/spotify",
        Path.home() / ".config/spotify",
    ):
        for f in [base / "prefs", *sorted(base.glob("Users/*/prefs"))]:
            try:
                for line in f.read_text(errors="replace").splitlines():
                    if "=" in line:
                        k, v = line.split("=", 1)
                        prefs[k.strip()] = v.strip()
            except OSError:
                pass
    return prefs


def checks() -> list[tuple[str, str, str]]:
    res: list[tuple[str, str, str]] = []
    info = _out("pactl", "info")
    if "PipeWire" in info:
        res.append((OK, "PipeWire", info.split("Server Name:", 1)[1].splitlines()[0].strip()))
    elif info:
        res.append((WARN, "Audio server", "not PipeWire – recording is untested"))
    else:
        res.append((FAIL, "Audio server", "pactl missing or no server reachable"))
    for tool in ("pw-record", "pw-metadata", "ffmpeg", "ffprobe"):
        res.append((OK if shutil.which(tool) else FAIL, tool, shutil.which(tool) or "missing"))
    if shutil.which("ffmpeg"):
        enc = _out("ffmpeg", "-hide_banner", "-encoders")
        res.append((OK if "libmp3lame" in enc else FAIL, "MP3 encoder", "libmp3lame" if "libmp3lame" in enc else "missing"))

    cmd = spotify_command()
    res.append((OK if cmd else FAIL, "Spotify", " ".join(cmd) if cmd else "not installed"))
    prefs = spotify_prefs()
    if prefs.get("audio.crossfade_v2") == "true":
        res.append((WARN, "Spotify: Crossfade", "on – blends into the next track (Settings → Playback)"))
    else:
        res.append((OK, "Spotify: Crossfade", "off"))
    if prefs.get("audio.normalize_v2", "true") != "false":
        res.append((WARN, "Spotify: Normalize volume", "on – changes the level slightly (fine for speech)"))
    quality = {"0": "automatic", "1": "low", "2": "normal", "3": "high", "4": "very high"}
    q = prefs.get("audio.play_bitrate_enumeration")
    res.append(
        (OK if q in ("3", "4") else WARN, "Spotify: Streaming quality",
         quality.get(q, "automatic") + ("" if q in ("3", "4") else " – \"Very high\" recommended"))
    )
    kde = "KDE" in os.environ.get("XDG_CURRENT_DESKTOP", "").upper()
    res.append((OK if kde else WARN, "Minimize Spotify", "via KWin script" if kde else "KDE Plasma only"))
    return res


def print_report(console) -> int:
    from rich.table import Table

    t = Table(title="Reelcast – System check", show_header=False, box=None, padding=(0, 1))
    icon = {OK: "[green]✓[/]", WARN: "[yellow]![/]", FAIL: "[red]✗[/]"}
    worst = OK
    for state, name, detail in checks():
        t.add_row(icon[state], f"[bold]{name}[/]", detail)
        if state == FAIL or (state == WARN and worst == OK):
            worst = state
    console.print(t)
    return 1 if worst == FAIL else 0
