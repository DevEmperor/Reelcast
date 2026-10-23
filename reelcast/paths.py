"""Ablage: wohin welche Aufnahme kommt, und wie die Ordner aussehen.

    <Ziel>/Podcasts/<Show>/<Folgentitel>.mp3
    <Ziel>/Musik/<Interpret>/<Album>/<Nr> - <Titel>.mp3
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .model import safe_filename

ICON_SRC = Path(__file__).with_name("assets") / "reelcast.svg"
ICON_DST = Path.home() / ".local/share/icons/hicolor/scalable/apps/reelcast.svg"


def default_out_dir() -> Path:
    try:
        music = subprocess.run(["xdg-user-dir", "MUSIC"], capture_output=True, text=True).stdout.strip()
    except FileNotFoundError:
        music = ""
    base = Path(music) if music and Path(music) != Path.home() else Path.home() / "Music"
    return base / "Reelcast"


def episode_path(root: Path, show: str, title: str, ext: str) -> Path:
    folder = root / "Podcasts" / safe_filename(show or "Podcast")
    return folder / f"{safe_filename(title)}.{ext}"


def track_path(root: Path, artist: str, album: str, title: str, number: int, disc: int, ext: str) -> Path:
    folder = root / "Musik" / safe_filename(artist or "Unbekannt")
    if album:
        folder = folder / safe_filename(album)
    prefix = ""
    if number:
        prefix = f"{disc}-{number:02d} - " if disc and disc > 1 else f"{number:02d} - "
    return folder / f"{prefix}{safe_filename(title)}.{ext}"


def install_icon() -> None:
    """Reelcast-Icon ins Benutzer-Icon-Theme legen (für Ordner, Starter, Fenster)."""
    try:
        if not ICON_DST.exists() or ICON_DST.read_bytes() != ICON_SRC.read_bytes():
            ICON_DST.parent.mkdir(parents=True, exist_ok=True)
            ICON_DST.write_bytes(ICON_SRC.read_bytes())
    except OSError:
        pass


def _dot_directory(folder: Path, icon: str) -> None:
    """Ordnersymbol für Dolphin & Co. – vorhandene Einstellungen bleiben unangetastet."""
    f = folder / ".directory"
    if f.exists():
        return
    try:
        f.write_text(f"[Desktop Entry]\nIcon={icon}\n")
    except OSError:
        pass


def decorate(root: Path, item_dir: Path, cover: bytes | None) -> None:
    """Reelcast-Ordner bekommt das Kassetten-Icon, Show- und Albumordner ihr Cover."""
    install_icon()
    root.mkdir(parents=True, exist_ok=True)
    _dot_directory(root, "reelcast")
    if cover and item_dir != root:
        art = item_dir / "cover.jpg"
        if not art.exists():
            try:
                art.write_bytes(cover)
            except OSError:
                return
        _dot_directory(item_dir, "./cover.jpg")
