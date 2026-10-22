"""Ablaufsteuerung für eine oder mehrere Folgen (von CLI und GUI genutzt)."""

from __future__ import annotations

import mimetypes
import urllib.parse
from pathlib import Path

from . import encode, feeds, recorder
from .model import (
    CancelToken,
    Episode,
    ReelcastError,
    Options,
    Reporter,
    Result,
    fmt_duration,
    safe_filename,
)
from .player import Spotify
from .spotify_meta import fetch_episode, parse_episode_id


def default_out_dir() -> Path:
    import subprocess

    try:
        music = subprocess.run(["xdg-user-dir", "MUSIC"], capture_output=True, text=True).stdout.strip()
    except FileNotFoundError:
        music = ""
    base = Path(music) if music and Path(music) != Path.home() else Path.home() / "Musik"
    return base / "Reelcast"


ICON_SRC = Path(__file__).with_name("assets") / "reelcast.svg"
ICON_DST = Path.home() / ".local/share/icons/hicolor/scalable/apps/reelcast.svg"


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


def decorate_folders(root: Path, show_dir: Path | None, cover: bytes | None) -> None:
    """Reelcast-Ordner bekommt das Kassetten-Icon, jeder Podcast-Ordner sein Cover."""
    install_icon()
    _dot_directory(root, "reelcast")
    if show_dir and show_dir != root and cover:
        art = show_dir / "cover.jpg"
        if not art.exists():
            try:
                art.write_bytes(cover)
            except OSError:
                return
        _dot_directory(show_dir, "./cover.jpg")


class Engine:
    def __init__(self, opts: Options, reporter: Reporter, cancel: CancelToken | None = None):
        self.opts = opts
        self.reporter = reporter
        self.cancel = cancel or CancelToken()
        self._spotify: Spotify | None = None

    # ---- öffentlich ---------------------------------------------------------

    def resolve(self, link: str) -> Episode:
        eid = parse_episode_id(link)
        self.reporter.phase("Lese Folgendaten …")
        ep = fetch_episode(eid)
        if not ep.title:
            raise ReelcastError("Spotify liefert keinen Titel für diese Folge.")
        self.reporter.episode(ep)
        return ep

    def target_path(self, ep: Episode) -> Path:
        folder = self.opts.out_dir
        if self.opts.folder_per_show and ep.show:
            folder = folder / safe_filename(ep.show)
        return folder / f"{safe_filename(ep.title)}.{self.opts.fmt}"

    def run(self, link: str) -> Result:
        ep = self.resolve(link)
        dest = self.target_path(ep)
        if dest.exists() and not self.opts.overwrite:
            dur, _ = encode.probe(dest)
            return Result(dest, "vorhanden", dur, skipped=True, note="existiert bereits")
        dest.parent.mkdir(parents=True, exist_ok=True)
        cover = encode.fetch_cover(ep.cover_url) or encode.fetch_cover(feeds.show_artwork_url(ep.show))
        decorate_folders(self.opts.out_dir, dest.parent, cover)

        if self.opts.mode in ("auto", "feed"):
            hit = feeds.find_episode(ep, self.reporter, self.cancel)
            if hit:
                return self._from_feed(ep, hit, dest, cover)
            if self.opts.mode == "feed":
                raise ReelcastError("Die Folge ist in keinem öffentlichen Feed zu finden.")
            self.reporter.info("→ Nehme die Folge über Spotify auf (Echtzeit).")
        if not ep.playable:
            raise ReelcastError("Spotify meldet die Folge als nicht abspielbar.")
        return self._from_spotify(ep, dest, cover)

    def close(self) -> None:
        """Am Ende aller Aufträge: selbst gestartetes Spotify wieder schließen."""
        if self._spotify:
            try:
                self._spotify.quit()
            finally:
                self._spotify.close()
                self._spotify = None

    # ---- Wege ---------------------------------------------------------------

    def _from_feed(self, ep: Episode, hit: feeds.FeedHit, dest: Path, cover) -> Result:
        host = urllib.parse.urlsplit(hit.feed_url or hit.audio_url).netloc
        self.reporter.info(f"Gefunden im öffentlichen Feed ({host}): {hit.title}")
        self.reporter.phase("Lade Originaldatei …")
        ext = Path(urllib.parse.urlsplit(hit.audio_url).path).suffix.lower()
        if ext not in (".mp3", ".m4a", ".mp4", ".aac", ".ogg", ".opus", ".flac", ".wav"):
            ext = mimetypes.guess_extension(hit.mime or "") or ".audio"
        tmp = dest.with_name(f".reelcast-{ep.id}{ext}")
        try:
            from . import net

            net.download(hit.audio_url, tmp, self.reporter, self.cancel)
            dur, codec = encode.probe(tmp)
            want_codec = encode.FORMATS[self.opts.fmt][1]
            copy = codec == want_codec
            if ep.duration and abs(dur - ep.duration) > max(20, 0.05 * ep.duration):
                self.reporter.warn(
                    f"Feed-Datei ist {fmt_duration(dur)} lang, Spotify-Fassung {fmt_duration(ep.duration)}."
                )
            self.reporter.phase("Schreibe Tags und Cover …" if copy else "Kodiere …")
            encode.finalize(
                tmp, dest, ep, self.opts.fmt, self.opts.quality, cover, self.reporter, self.cancel,
                copy=copy, source_note=f"Quelle: Podcast-Feed {host}",
            )
        finally:
            tmp.unlink(missing_ok=True)
        dur, _ = encode.probe(dest)
        note = "Originaldatei, nicht neu kodiert" if copy else f"aus {codec} umgewandelt"
        return Result(dest, "feed", dur, note=note)

    def _from_spotify(self, ep: Episode, dest: Path, cover) -> Result:
        if self._spotify is None:
            self._spotify = Spotify()
        flac = dest.with_name(f".reelcast-{ep.id}.flac")
        try:
            rec = recorder.record(self._spotify, ep, self.opts, self.reporter, self.cancel, flac)
            self.reporter.phase("Kodiere …")
            encode.finalize(
                flac, dest, ep, self.opts.fmt, self.opts.quality, cover, self.reporter, self.cancel,
                segments=rec.segments, source_note="Quelle: Aufnahme der Spotify-Wiedergabe",
            )
        finally:
            flac.unlink(missing_ok=True)
        dur, _ = encode.probe(dest)
        note = ""
        if dur < rec.expected - 5:
            note = f"kürzer als erwartet ({fmt_duration(dur)} von {fmt_duration(rec.expected)})"
            self.reporter.warn(f"Aufnahme ist {note}.")
        return Result(dest, "spotify", dur, note=note or "aufgenommen")
