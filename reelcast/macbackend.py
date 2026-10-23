"""macOS backend: Spotify's audio via a Core Audio process tap, control via AppleScript.

Both go through the bundled `reelcast-helper` (Swift). macOS therefore asks only
twice, on behalf of Reelcast: "record system audio" and "control Spotify".
"""

from __future__ import annotations

import atexit
import json
import subprocess
import threading
import time

from . import tools
from .model import CancelToken, ReelcastError

NOT_AUTHORIZED = -1743
STATUS = {"playing": "Playing", "paused": "Paused", "stopped": "Stopped"}

AUTOMATION_HINT = (
    "Reelcast isn't allowed to control Spotify. Allow it under System Settings → "
    "Privacy & Security → Automation → Reelcast → Spotify."
)
AUDIO_HINT = (
    "Reelcast isn't allowed to record Spotify's audio. Allow it under System Settings → "
    "Privacy & Security → Screen & System Audio Recording."
)


def _control(command: str, arg: str = "") -> str:
    """Run a Spotify command through the helper; returns an error message or ""."""
    args = [tools.helper(), "control", command] + ([arg] if arg else [])
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        return str(e)
    if p.returncode == 0:
        return ""
    for line in p.stderr.splitlines():
        try:
            msg = json.loads(line).get("message", "")
        except ValueError:
            continue
        if "Not authorized" in msg:
            return AUTOMATION_HINT
        return msg
    return p.stderr.strip() or f"helper exited with {p.returncode}"


class _StateReader:
    """One helper process that streams Spotify's state; shared by sink and player."""

    _instance: "_StateReader | None" = None
    _lock = threading.Lock()

    @classmethod
    def get(cls) -> "_StateReader":
        with cls._lock:
            if cls._instance is None or cls._instance.proc.poll() is not None:
                cls._instance = cls()
                atexit.register(cls._instance.close)
            return cls._instance

    def __init__(self) -> None:
        self.state: dict = {}
        self.updated = threading.Event()
        self.proc = subprocess.Popen(
            [tools.helper(), "state", "--interval", "0.1"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        threading.Thread(target=self._read, name="spotify-state", daemon=True).start()

    def _read(self) -> None:
        for line in self.proc.stdout:
            try:
                st = json.loads(line)
            except ValueError:
                continue
            st["_mono"] = time.monotonic()
            self.state = st
            self.updated.set()

    def latest(self) -> dict:
        if not self.state:
            self.updated.wait(3.0)
        return dict(self.state)

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()


class MacSpotify:
    """Same interface as the Linux MPRIS player (player.Spotify)."""

    def __init__(self) -> None:
        self.launched_by_us = False
        self.last_error = ""

    # ---- state ----------------------------------------------------------------

    def _state(self) -> dict:
        st = _StateReader.get().latest()
        if st.get("errorNumber") == NOT_AUTHORIZED:
            self.last_error = AUTOMATION_HINT
        elif "error" in st:
            self.last_error = str(st["error"])
        else:
            self.last_error = ""
        return st

    def running(self) -> bool:
        return bool(self._state().get("running"))

    def snapshot(self) -> tuple[str, dict, float | None]:
        st = self._state()
        if not st.get("running") or "status" not in st:
            return "Unknown", {}, None
        status = STATUS.get(st["status"], "Unknown")
        pos = float(st.get("position") or 0.0)
        if status == "Playing":
            pos += time.monotonic() - st["_mono"]  # the sample is up to 100 ms old
        return status, _meta(st), pos

    def metadata(self) -> dict:
        return self.snapshot()[1]

    def status(self) -> str:
        return self.snapshot()[0]

    def position(self) -> float | None:
        return self.snapshot()[2]

    def volume(self) -> float | None:
        st = self._state()
        vol = st.get("volume")
        return vol / 100.0 if isinstance(vol, int) and st.get("running") else None

    def set_volume(self, value: float) -> bool:
        return not _control("volume", str(int(round(value * 100))))

    @staticmethod
    def matches(meta: dict, episode_id: str) -> bool:
        return episode_id in str(meta.get("mpris:trackid", "")) or episode_id in str(
            meta.get("xesam:url", "")
        )

    # ---- commands -----------------------------------------------------------------

    def open_uri(self, uri: str) -> None:
        err = _control("open", uri)
        if err:
            raise ReelcastError(err)

    def play(self) -> None:
        err = _control("play")
        if err:
            raise ReelcastError(err)

    def pause(self) -> None:
        _control("pause")

    def set_position(self, trackid: str, seconds: float) -> bool:
        return not _control("seek", f"{seconds:.3f}")

    # ---- start / end ----------------------------------------------------------------

    def ensure_running(self, cancel: CancelToken, minimize: bool, timeout: float = 45) -> None:
        if self.running():
            return
        args = ["open", "-g", "-a", "Spotify"]
        if minimize:
            args.insert(2, "-j")  # launch hidden
        if subprocess.run(args, capture_output=True).returncode != 0:
            raise ReelcastError("Spotify isn't installed (expected /Applications/Spotify.app).")
        self.launched_by_us = True
        deadline = time.monotonic() + timeout
        while not self.running():
            if time.monotonic() > deadline:
                raise ReelcastError("Spotify didn't start (timed out).")
            cancel.sleep(0.3)
        cancel.sleep(3.0)  # the client needs a moment before it accepts commands
        if self.last_error:
            raise ReelcastError(self.last_error)

    def release_window(self) -> None:
        pass

    def quit(self) -> None:
        if self.launched_by_us:
            _control("quit")

    def close(self) -> None:
        pass


def _meta(st: dict) -> dict:
    """Spotify's AppleScript state in the shape of MPRIS metadata (what the session expects)."""
    uri = str(st.get("id") or "")
    parts = uri.split(":")
    if len(parts) < 3:
        return {}
    kind, ident = parts[1], parts[-1]
    artist = str(st.get("artist") or "")
    album_artist = str(st.get("albumArtist") or "")
    return {
        "mpris:trackid": f"/com/spotify/{kind}/{ident}",
        "mpris:length": int(st.get("durationMs") or 0) * 1000,
        "mpris:artUrl": str(st.get("artworkUrl") or ""),
        "xesam:title": str(st.get("name") or ""),
        "xesam:artist": [artist] if artist else [],
        "xesam:albumArtist": [album_artist] if album_artist else [],
        "xesam:album": str(st.get("album") or ""),
        "xesam:trackNumber": int(st.get("trackNumber") or 0),
        "xesam:discNumber": int(st.get("discNumber") or 0),
        "xesam:url": f"https://open.spotify.com/{kind}/{ident}",
    }


class MacSink:
    """Counterpart of the Linux CaptureSink.

    There is nothing to reroute on macOS: the helper taps Spotify's processes
    directly. Without `listen`, Spotify is muted while it is being recorded.
    """

    name = "reelcast"

    def __init__(self, listen: bool = False):
        self.listen = listen
        self.lock = threading.Lock()
        self._processes = 0
        self._new_taps = 0  # for route()
        self._late = 0  # for take_late_routes()
        self._orig_volume: int | None = None

    def __enter__(self) -> "MacSink":
        return self

    def __exit__(self, *exc) -> None:
        self.restore()

    def source_command(self) -> list[str]:
        return [tools.helper(), "tap"] + (["--listen"] if self.listen else [])

    def on_source_event(self, event: dict) -> str | None:
        kind = event.get("event")
        if kind == "tap":
            n = int(event.get("processes") or 0)
            with self.lock:
                if n > 0:
                    self._new_taps += 1
                    self._late += 1
                self._processes = n
        elif kind == "error":
            message = str(event.get("message", ""))
            if "CreateProcessTap" in message:
                return AUDIO_HINT
            return message
        return None

    def route(self) -> tuple[int, int]:
        with self.lock:
            newly, self._new_taps = self._new_taps, 0
            return self._processes, newly

    def take_late_routes(self) -> int:
        with self.lock:
            n, self._late = self._late, 0
            return n

    def watch(self) -> None:
        pass

    def enforce_full_volume(self) -> bool:
        """Spotify's own volume scales what the tap records – keep it at 100 %."""
        st = _StateReader.get().latest()
        vol = st.get("volume")
        if st.get("running") and isinstance(vol, int) and vol != 100:
            if self._orig_volume is None:
                self._orig_volume = vol
            _control("volume", "100")
            return True
        return False

    def restore(self) -> None:
        if self._orig_volume is not None:
            _control("volume", str(self._orig_volume))
            self._orig_volume = None
