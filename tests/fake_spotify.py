"""Fake Spotify player for tests (MPRIS over D-Bus + audio via pw-play).

Plays a sequence of synthetic "tracks" (sine tones) as *one* continuous audio
file and reports track, position and status over MPRIS – just like Spotify.
Pauses are silence in the audio file; meanwhile the player reports "Paused" and
holds the position – like Spotify, which stops audio and position together.

Usage: python3 fake_spotify.py <work dir>
Writes <work dir>/expected.json with the expected results and exits once
everything has played.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

BUS = os.environ.get("REELCAST_MPRIS_NAME", "org.mpris.MediaPlayer2.reelcasttest")
APP = "ReelcastFake"
RATE = 48000

# (key, kind, title, length, frequency, played until, pauses [(at, duration)], silences [(from, to)])
PLAN = [
    ("t1", "track", "First Track", 5.0, 440, 5.0, [], []),
    ("t2", "track", "Second Track", 4.0, 660, 4.0, [], [(3.5, 4.0)]),
    ("t3", "track", "Third Track", 6.0, 880, 6.0, [(2.0, 2.0)], [(3.5, 4.7)]),
    ("ad", "ad", "Advertisement", 3.0, 300, 3.0, [], []),
    ("t4", "track", "Skipped Track", 5.0, 500, 1.5, [], []),
    ("t5", "track", "Fifth Track", 4.0, 1000, 4.0, [], []),
]

INTROSPECTION = """
<node>
  <interface name="org.mpris.MediaPlayer2.Player">
    <method name="Play"/><method name="Pause"/>
    <property name="PlaybackStatus" type="s" access="read"/>
    <property name="Metadata" type="a{sv}" access="read"/>
    <property name="Position" type="x" access="read"/>
  </interface>
</node>
"""


def tone(freq: float, seconds: float, silences) -> np.ndarray:
    t = np.arange(int(seconds * RATE)) / RATE
    y = 0.3 * np.sin(2 * np.pi * freq * t)
    for a, b in silences:
        y[int(a * RATE) : int(b * RATE)] = 0.0
    return np.stack([y, y], axis=1).astype("<f4")


class Player:
    def __init__(self, work: Path):
        self.work = work
        self.status = "Stopped"
        self.meta: dict = {}
        self.track_start = 0.0  # wall clock time at which the position was 0 (excluding pauses)
        self.paused_at: float | None = None
        self.lock = threading.Lock()

    def position(self) -> int:
        with self.lock:
            if self.status == "Stopped":
                return 0
            ref = self.paused_at if self.paused_at is not None else time.monotonic()
            return int((ref - self.track_start) * 1e6)

    def metadata_variant(self) -> GLib.Variant:
        with self.lock:
            m = dict(self.meta)
        out = {}
        for k, v in m.items():
            if k == "mpris:trackid":
                out[k] = GLib.Variant("o", v)
            elif k == "mpris:length":
                out[k] = GLib.Variant("x", v)
            elif k in ("xesam:artist", "xesam:albumArtist"):
                out[k] = GLib.Variant("as", v)
            elif k == "xesam:trackNumber":
                out[k] = GLib.Variant("i", v)
            else:
                out[k] = GLib.Variant("s", v)
        return GLib.Variant("a{sv}", out)

    def get_property(self, conn, sender, path, iface, prop):
        if prop == "PlaybackStatus":
            return GLib.Variant("s", self.status)
        if prop == "Metadata":
            return self.metadata_variant()
        if prop == "Position":
            return GLib.Variant("x", self.position())
        return None

    def method_call(self, conn, sender, path, iface, method, params, invocation):
        invocation.return_value(None)

    # ---- playback ---------------------------------------------------------

    def run(self, loop: GLib.MainLoop) -> None:
        try:
            self._play()
        finally:
            GLib.idle_add(loop.quit)

    def _play(self) -> None:
        audio = []
        expected = []
        for i, (key, kind, title, length, freq, played, pauses, silences) in enumerate(PLAN, 1):
            y = tone(freq, played, silences)
            for at, dur in sorted(pauses, reverse=True):
                i_at = int(at * RATE)
                y = np.concatenate([y[:i_at], np.zeros((int(dur * RATE), 2), dtype="<f4"), y[i_at:]])
            audio.append(y)
            if kind == "track" and played >= length:
                expected.append({"title": title, "length": length, "freq": freq, "number": i})
        wav = self.work / "fake.raw"
        np.concatenate(audio).tofile(wav)
        (self.work / "expected.json").write_text(json.dumps(expected, ensure_ascii=False))

        time.sleep(1.5)  # give Reelcast time to connect
        target = ["--target", os.environ["FAKE_TARGET"]] if os.environ.get("FAKE_TARGET") else []
        proc = subprocess.Popen(
            ["pw-play", *target, "--raw", "--rate", str(RATE), "--channels", "2", "--format", "f32",
             "-P", f"application.name={APP}", "--latency", "50ms", str(wav)],
        )
        t0 = time.monotonic()
        offset = 0.0  # start of the track within the audio file
        for i, (key, kind, title, length, freq, played, pauses, silences) in enumerate(PLAN, 1):
            with self.lock:
                self.track_start = t0 + offset
                self.paused_at = None
                self.status = "Playing"
                self.meta = {
                    "mpris:trackid": f"/com/spotify/{kind}/{key}fake0000000000000000"[:40],
                    "mpris:length": int(length * 1e6),
                    "xesam:title": title,
                    "xesam:artist": ["Test Band"],
                    "xesam:albumArtist": ["Test Band"],
                    "xesam:album": "Test Album",
                    "xesam:trackNumber": i,
                    "xesam:url": f"https://open.spotify.com/{kind}/{key}fake0000000000000000",
                }
            shift = 0.0
            for at, dur in pauses:
                _sleep_until(t0 + offset + at)
                with self.lock:
                    self.status, self.paused_at = "Paused", t0 + offset + at
                _sleep_until(t0 + offset + at + dur)
                with self.lock:
                    self.track_start += dur
                    self.paused_at = None
                    self.status = "Playing"
                shift += dur
            _sleep_until(t0 + offset + shift + played)
            offset += played + shift
        with self.lock:
            self.status = "Stopped"
            self.meta = {}
        proc.wait()
        time.sleep(4.0)  # let Reelcast write the rest


def _sleep_until(t: float) -> None:
    d = t - time.monotonic()
    if d > 0:
        time.sleep(d)


def main() -> None:
    work = Path(sys.argv[1])
    work.mkdir(parents=True, exist_ok=True)
    player = Player(work)
    loop = GLib.MainLoop()
    node = Gio.DBusNodeInfo.new_for_xml(INTROSPECTION)

    def on_bus(conn, name):
        conn.register_object("/org/mpris/MediaPlayer2", node.interfaces[0],
                             player.method_call, player.get_property, None)

    def on_name(conn, name):
        threading.Thread(target=player.run, args=(loop,), daemon=True).start()

    Gio.bus_own_name(Gio.BusType.SESSION, BUS, Gio.BusNameOwnerFlags.NONE, on_bus, on_name, None)
    loop.run()


if __name__ == "__main__":
    main()
