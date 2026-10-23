"""Spotify-Desktop über MPRIS (D-Bus) fernsteuern – keine Klicks, kein Fenster nötig.

D-Bus läuft über GIO (PyGObject), das auf jedem GNOME/KDE-System ohnehin da ist.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

from .model import CancelToken, ReelcastError  # noqa: E402

# Für Tests lässt sich ein anderer MPRIS-Player unterschieben
BUS_NAME = os.environ.get("REELCAST_MPRIS_NAME", "org.mpris.MediaPlayer2.spotify")
OBJ_PATH = "/org/mpris/MediaPlayer2"
PLAYER = "org.mpris.MediaPlayer2.Player"
ROOT = "org.mpris.MediaPlayer2"
PROPS = "org.freedesktop.DBus.Properties"
FLATPAK_ID = "com.spotify.Client"


class Bus:
    """Dünne, synchrone Hülle um die Session-Bus-Verbindung (threadsicher)."""

    def __init__(self) -> None:
        try:
            self.conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error as e:
            raise ReelcastError(f"Keine Verbindung zum D-Bus: {e.message}") from e

    def call(self, dest: str, path: str, iface: str, method: str, args: GLib.Variant | None = None,
             timeout_ms: int = 10000):
        reply = self.conn.call_sync(
            dest, path, iface, method, args, None, Gio.DBusCallFlags.NONE, timeout_ms, None
        )
        return reply.unpack() if reply is not None else ()

    def has_owner(self, name: str) -> bool:
        try:
            return bool(
                self.call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                          "NameHasOwner", GLib.Variant("(s)", (name,)))[0]
            )
        except GLib.Error:
            return False


def spotify_command() -> list[str] | None:
    if shutil.which("flatpak"):
        r = subprocess.run(["flatpak", "info", FLATPAK_ID], capture_output=True)
        if r.returncode == 0:
            return ["flatpak", "run", FLATPAK_ID]
    for exe in ("spotify", "spotify-launcher"):
        if shutil.which(exe):
            return [exe]
    return None


class Spotify:
    def __init__(self) -> None:
        self.bus = Bus()
        self.launched_by_us = False
        self._proc: subprocess.Popen | None = None
        self._hider: KWinHider | None = None

    def close(self) -> None:
        self.release_window()

    # ---- Grundlagen -------------------------------------------------------

    def running(self) -> bool:
        return self.bus.has_owner(BUS_NAME)

    def _get(self, prop: str, iface: str = PLAYER):
        return self.bus.call(BUS_NAME, OBJ_PATH, PROPS, "Get",
                             GLib.Variant("(ss)", (iface, prop)), timeout_ms=5000)[0]

    def _set(self, prop: str, value: GLib.Variant, iface: str = PLAYER) -> None:
        self.bus.call(BUS_NAME, OBJ_PATH, PROPS, "Set",
                      GLib.Variant("(ssv)", (iface, prop, value)), timeout_ms=5000)

    def _call(self, method: str, args: GLib.Variant | None = None, iface: str = PLAYER) -> None:
        self.bus.call(BUS_NAME, OBJ_PATH, iface, method, args)

    # ---- Zustand ------------------------------------------------------------

    def snapshot(self) -> tuple[str, dict, float | None]:
        """(PlaybackStatus, Metadata, Position in s) mit einem einzigen D-Bus-Aufruf."""
        try:
            props = self.bus.call(BUS_NAME, OBJ_PATH, PROPS, "GetAll",
                                  GLib.Variant("(s)", (PLAYER,)), timeout_ms=3000)[0]
        except GLib.Error:
            return "Unknown", {}, None
        pos = props.get("Position")
        return (
            str(props.get("PlaybackStatus", "Unknown")),
            dict(props.get("Metadata") or {}),
            pos / 1e6 if isinstance(pos, int) else None,
        )

    def metadata(self) -> dict:
        try:
            return dict(self._get("Metadata"))
        except GLib.Error:
            return {}

    def status(self) -> str:
        try:
            return str(self._get("PlaybackStatus"))
        except GLib.Error:
            return "Unknown"

    def position(self) -> float | None:
        try:
            return self._get("Position") / 1e6
        except GLib.Error:
            return None

    def volume(self) -> float | None:
        try:
            return float(self._get("Volume"))
        except GLib.Error:
            return None

    def set_volume(self, value: float) -> bool:
        try:
            self._set("Volume", GLib.Variant("d", float(value)))
            return True
        except GLib.Error:
            return False

    @staticmethod
    def matches(meta: dict, episode_id: str) -> bool:
        return episode_id in str(meta.get("mpris:trackid", "")) or episode_id in str(
            meta.get("xesam:url", "")
        )

    # ---- Befehle --------------------------------------------------------------

    def open_uri(self, uri: str) -> None:
        self._call("OpenUri", GLib.Variant("(s)", (uri,)))

    def play(self) -> None:
        self._call("Play")

    def pause(self) -> None:
        try:
            self._call("Pause")
        except GLib.Error:
            pass

    def set_position(self, trackid: str, seconds: float) -> bool:
        try:
            self._call("SetPosition", GLib.Variant("(ox)", (trackid, int(seconds * 1e6))))
            return True
        except GLib.Error:
            try:
                self._call("Seek", GLib.Variant("(x)", (-int(1e12),)))
                return True
            except GLib.Error:
                return False

    # ---- Start / Ende ---------------------------------------------------------

    def ensure_running(self, cancel: CancelToken, minimize: bool, timeout: float = 45) -> None:
        if self.running():
            return
        cmd = spotify_command()
        if not cmd:
            raise ReelcastError(
                "Spotify ist nicht installiert (weder Flatpak com.spotify.Client noch 'spotify')."
            )
        if minimize:
            # bleibt aktiv, bis die Wiedergabe läuft (release_window), damit auch ein spät
            # erscheinendes Fenster erfasst wird
            self._hider = KWinHider(self.bus)
            self._hider.start()
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.launched_by_us = True
        deadline = time.monotonic() + timeout
        while not self.running():
            if time.monotonic() > deadline:
                raise ReelcastError("Spotify hat sich nicht am D-Bus gemeldet (Zeitüberschreitung).")
            cancel.sleep(0.3)
        cancel.sleep(1.5)  # MPRIS ist da, der Client braucht noch einen Moment

    def release_window(self) -> None:
        if self._hider:
            self._hider.stop()
            self._hider = None

    def quit(self) -> None:
        """Nur ein selbst gestartetes Spotify wieder schließen."""
        self.release_window()
        if not self.launched_by_us:
            return
        try:
            self._call("Quit", iface=ROOT)
        except GLib.Error:
            pass
        for _ in range(30):
            if not self.running():
                return
            time.sleep(0.2)
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()


class KWinHider:
    """Minimiert das Spotify-Fenster unter KDE Plasma per KWin-Skript (best effort)."""

    SCRIPT = """
function hide(w) {
    if (!w) return;
    var cls = String(w.resourceClass || "") + " " + String(w.desktopFileName || "");
    if (cls.toLowerCase().indexOf("spotify") >= 0 && !w.minimized) w.minimized = true;
}
workspace.windowList().forEach(hide);
workspace.windowAdded.connect(hide);
"""
    PLUGIN = "reelcast-hide-spotify"
    IFACE = "org.kde.kwin.Scripting"

    def __init__(self, bus: Bus) -> None:
        self.bus = bus
        self.path: Path | None = None
        self.ok = False

    def _scripting(self, method: str, args: GLib.Variant | None = None):
        return self.bus.call("org.kde.KWin", "/Scripting", self.IFACE, method, args, timeout_ms=3000)

    def start(self) -> None:
        if "KDE" not in os.environ.get("XDG_CURRENT_DESKTOP", "").upper():
            return
        try:
            fd, name = tempfile.mkstemp(prefix="reelcast-", suffix=".js")
            with os.fdopen(fd, "w") as f:
                f.write(self.SCRIPT)
            self.path = Path(name)
            self._scripting("unloadScript", GLib.Variant("(s)", (self.PLUGIN,)))
            self._scripting("loadScript", GLib.Variant("(ss)", (str(self.path), self.PLUGIN)))
            self._scripting("start")
            self.ok = True
        except GLib.Error:
            self.ok = False

    def stop(self) -> None:
        try:
            if self.ok:
                self._scripting("unloadScript", GLib.Variant("(s)", (self.PLUGIN,)))
        except GLib.Error:
            pass
        if self.path:
            self.path.unlink(missing_ok=True)
            self.path = None
