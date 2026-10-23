"""The PipeWire side of recording.

* `CaptureSink` creates a virtual output, routes *only* Spotify's stream to it and
  cleans everything up afterwards (stream back, volume back, module unloaded).
  System sounds and other apps never end up in a recording.
* `Capture` reads that output's monitor losslessly (float32, 48 kHz), measures
  level and digital silence live and writes a FLAC file alongside.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from array import array
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .model import ReelcastError

RATE = 48000  # PipeWire graph rate; this way audio is resampled only once (from Spotify's rate)
CHANNELS = 2
CHUNK_FRAMES = RATE // 10  # 100 ms
SILENCE_EPS = 1e-6  # ≈ −120 dBFS: only true digital silence (pause, buffering)
GAP_MIN = 1.0  # digital silence this long counts as a gap and is removed

SINK_NAME = "reelcast"
APP_MATCH = os.environ.get("REELCAST_APP_MATCH", "spotify").lower()


def _run(*args: str, check: bool = True) -> str:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=15)
    except FileNotFoundError as e:
        raise ReelcastError(f"Missing program: {args[0]}") from e
    if check and p.returncode != 0:
        raise ReelcastError(f"{' '.join(args[:3])} …: {p.stderr.strip() or p.returncode}")
    return p.stdout


def pactl_json(*args: str):
    return json.loads(_run("pactl", "-f", "json", *args) or "[]")


def _is_app_stream(si: dict) -> bool:
    props = si.get("properties", {})
    keys = (
        "application.name",
        "application.process.binary",
        "application.id",
        "pipewire.access.portal.app_id",
        "node.name",
    )
    return any(APP_MATCH in str(props.get(k, "")).lower() for k in keys)


def _node_id(si: dict) -> str | None:
    return si.get("properties", {}).get("object.id")


def _target_meta(node_id: str) -> str | None:
    """Stored target (target.object) of a stream; None = follows the default output."""
    out = _run("pw-metadata", "-n", "default", node_id, "target.object", check=False)
    for line in out.splitlines():
        if "key:'target.object'" in line and "value:'" in line:
            return line.split("value:'", 1)[1].split("'", 1)[0] or None
    return None


@dataclass
class _Moved:
    index: int
    node_id: str | None
    orig_sink: str
    orig_target: str | None
    orig_volume: list[int]
    orig_mute: bool


@dataclass
class CaptureSink:
    listen: bool = False
    name: str = SINK_NAME
    modules: list[str] = field(default_factory=list)
    moved: dict[int, _Moved] = field(default_factory=dict)
    lock: threading.RLock = field(default_factory=threading.RLock)
    _watcher: subprocess.Popen | None = None
    _late_routes: int = 0

    # ---- Setup and teardown ---------------------------------------------

    def __enter__(self) -> "CaptureSink":
        self.remove_stale()
        mod = _run(
            "pactl",
            "load-module",
            "module-null-sink",
            f"sink_name={self.name}",
            "sink_properties=device.description=Reelcast-Recorder",
            f"rate={RATE}",
            f"channels={CHANNELS}",
        ).strip()
        self.modules.append(mod)
        if self.listen:
            self.modules.append(
                _run(
                    "pactl",
                    "load-module",
                    "module-loopback",
                    f"source={self.name}.monitor",
                    "sink=@DEFAULT_SINK@",
                    "latency_msec=80",
                ).strip()
            )
        return self

    def __exit__(self, *exc) -> None:
        if self._watcher and self._watcher.poll() is None:
            self._watcher.terminate()
        self.restore()
        for mod in reversed(self.modules):
            _run("pactl", "unload-module", mod, check=False)
        self.modules.clear()

    def remove_stale(self) -> None:
        """Remove leftovers from a crashed run."""
        for m in pactl_json("list", "modules"):
            arg = m.get("argument") or ""
            if m.get("name") in ("module-null-sink", "module-loopback") and (
                f"sink_name={self.name}" in arg or f"source={self.name}.monitor" in arg
            ):
                _run("pactl", "unload-module", str(m.get("index")), check=False)

    # ---- Spotify's stream ---------------------------------------------------

    def _sinks(self) -> dict[int, str]:
        return {s["index"]: s["name"] for s in pactl_json("list", "sinks")}

    def app_streams(self) -> list[dict]:
        return [si for si in pactl_json("list", "sink-inputs") if _is_app_stream(si)]

    def watch(self) -> None:
        """Route new streams immediately (pactl subscribe) instead of waiting for the next check."""
        self._watcher = subprocess.Popen(
            ["pactl", "subscribe"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
        )

        def loop() -> None:
            for line in self._watcher.stdout:
                if "'new' on sink-input" in line or "'change' on sink-input" in line:
                    _, newly = self.route()
                    if newly:
                        with self.lock:
                            self._late_routes += newly

        threading.Thread(target=loop, name="route-watch", daemon=True).start()

    def take_late_routes(self) -> int:
        """How often the watcher had to route a stream since the last call."""
        with self.lock:
            n, self._late_routes = self._late_routes, 0
            return n

    def route(self) -> tuple[int, int]:
        """Move all Spotify streams to the recording output.

        Returns (number of streams, of which newly routed).
        """
        with self.lock:
            return self._route()

    def _route(self) -> tuple[int, int]:
        sinks = self._sinks()
        streams = self.app_streams()
        newly = 0
        for si in streams:
            idx = si["index"]
            if sinks.get(si.get("sink")) == self.name:
                continue
            if idx not in self.moved:
                vol = si.get("volume", {})
                node = _node_id(si)
                self.moved[idx] = _Moved(
                    index=idx,
                    node_id=node,
                    orig_sink=sinks.get(si.get("sink"), ""),
                    orig_target=_target_meta(node) if node else None,
                    orig_volume=[ch["value"] for ch in vol.values()] or [65536],
                    orig_mute=bool(si.get("mute")),
                )
            _run("pactl", "move-sink-input", str(idx), self.name, check=False)
            _run("pactl", "set-sink-input-volume", str(idx), "65536", check=False)
            _run("pactl", "set-sink-input-mute", str(idx), "0", check=False)
            newly += 1
        return len(streams), newly

    def enforce_full_volume(self) -> bool:
        """Keep Spotify streams on the recording output at 100 % (unaltered signal)."""
        with self.lock:
            return self._enforce_full_volume()

    def _enforce_full_volume(self) -> bool:
        changed = False
        sinks = self._sinks()
        for si in self.app_streams():
            if sinks.get(si.get("sink")) != self.name:
                continue
            values = [ch.get("value") for ch in si.get("volume", {}).values()]
            if any(v != 65536 for v in values) or si.get("mute"):
                _run("pactl", "set-sink-input-volume", str(si["index"]), "65536", check=False)
                _run("pactl", "set-sink-input-mute", str(si["index"]), "0", check=False)
                changed = True
        return changed

    def restore(self) -> None:
        """Put streams back where they were and restore their volume."""
        with self.lock:
            self._restore()

    def _restore(self) -> None:
        if not self.moved:
            return
        alive = {si["index"]: si for si in pactl_json("list", "sink-inputs")}
        sinks = set(self._sinks().values())
        for idx, mv in self.moved.items():
            if idx not in alive:
                continue
            target = mv.orig_sink if mv.orig_sink in sinks else "@DEFAULT_SINK@"
            _run("pactl", "move-sink-input", str(idx), target, check=False)
            if mv.orig_target is None and mv.node_id:
                # The stream used to follow the default output: drop the fixed binding again
                _run("pw-metadata", "-n", "default", "-d", mv.node_id, "target.object", check=False)
            _run("pactl", "set-sink-input-volume", str(idx), *map(str, mv.orig_volume), check=False)
            _run("pactl", "set-sink-input-mute", str(idx), "1" if mv.orig_mute else "0", check=False)
        self.moved.clear()


class Capture:
    """Records the recording output's monitor and analyses it live."""

    def __init__(self, sink_name: str, flac_path: Path, on_level=None):
        self.sink_name = sink_name
        self.flac_path = flac_path
        self.on_level = on_level
        self.lock = threading.Lock()
        self.frames = 0  # insgesamt geschriebene Frames
        self.cut_before = 0  # everything before this is discarded
        self.first_sound: int | None = None
        self.last_sound_end = 0
        self.gaps: list[tuple[int, int]] = []
        self.peak_db = -120.0
        self.error: str | None = None
        self._rec: subprocess.Popen | None = None
        self._enc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._started = threading.Event()

    # ---- Steuerung --------------------------------------------------------

    def start(self, timeout: float = 5.0) -> None:
        for tool in ("pw-record", "ffmpeg"):
            if not shutil.which(tool):
                raise ReelcastError(f"Missing program: {tool}")
        self._enc = subprocess.Popen(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "f32le", "-ar", str(RATE), "-ac", str(CHANNELS), "-i", "pipe:0",
                "-c:a", "flac", "-sample_fmt", "s32", "-bits_per_raw_sample", "24",
                "-compression_level", "3", "-f", "flac", str(self.flac_path),
            ],
            stdin=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._rec = subprocess.Popen(
            [
                "pw-record", "--target", self.sink_name,
                "-P", "stream.capture.sink=true",
                "-P", "node.description=Reelcast",
                "--rate", str(RATE), "--channels", str(CHANNELS), "--format", "f32",
                "--latency", "50ms", "--raw", "-",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self._thread = threading.Thread(target=self._pump, name="capture", daemon=True)
        self._thread.start()
        if not self._started.wait(timeout):
            self.stop()
            raise ReelcastError("Recording doesn't start (pw-record delivers no data).")

    def stop(self) -> None:
        if self._rec and self._rec.poll() is None:
            self._rec.terminate()
            try:
                self._rec.wait(3)
            except subprocess.TimeoutExpired:
                self._rec.kill()
        if self._thread:
            self._thread.join(5)
        if self._enc:
            try:
                if self._enc.stdin and not self._enc.stdin.closed:
                    self._enc.stdin.close()
            except BrokenPipeError:
                pass
            try:
                self._enc.wait(30)
            except subprocess.TimeoutExpired:
                self._enc.kill()
            if self._enc.returncode not in (0, None) and not self.error:
                err = (self._enc.stderr.read() if self._enc.stderr else b"").decode(errors="replace")
                self.error = f"FLAC-Encoder: {err.strip()[:300]}"

    def mark_restart(self) -> None:
        """Discard everything recorded so far (e.g. after jumping back to the start)."""
        with self.lock:
            self.cut_before = self.frames
            self.first_sound = None
            self.last_sound_end = self.frames
            self.gaps = []

    # ---- Auswertung -------------------------------------------------------

    @property
    def content_seconds(self) -> float:
        """Audible length since the first sound, without gaps."""
        with self.lock:
            if self.first_sound is None:
                return 0.0
            gaps = sum(b - a for a, b in self.gaps)
            return (self.last_sound_end - self.first_sound - gaps) / RATE

    @property
    def silent_for(self) -> float:
        """Seconds of digital silence at the end of the recording so far."""
        with self.lock:
            return (self.frames - max(self.last_sound_end, self.cut_before)) / RATE

    def segments(self, max_seconds: float | None = None) -> list[tuple[int, int]]:
        """Sections (frame ranges) of the FLAC file to keep."""
        with self.lock:
            if self.first_sound is None:
                return []
            segs: list[tuple[int, int]] = []
            pos = self.first_sound
            for a, b in self.gaps:
                if a > pos:
                    segs.append((pos, a))
                pos = b
            if self.last_sound_end > pos:
                segs.append((pos, self.last_sound_end))
        if max_seconds:
            limit = int(max_seconds * RATE)
            out, total = [], 0
            for a, b in segs:
                if total >= limit:
                    break
                b = min(b, a + (limit - total))
                out.append((a, b))
                total += b - a
            segs = out
        return segs

    # ---- Arbeitsthread ----------------------------------------------------

    def _pump(self) -> None:
        bytes_per_chunk = CHUNK_FRAMES * CHANNELS * 4
        stdout = self._rec.stdout
        stdin = self._enc.stdin
        buf = b""
        try:
            while True:
                data = stdout.read1(bytes_per_chunk) if hasattr(stdout, "read1") else stdout.read(bytes_per_chunk)
                if not data:
                    break
                buf += data
                usable = len(buf) - len(buf) % (CHANNELS * 4)
                if not usable:
                    continue
                block, buf = buf[:usable], buf[usable:]
                stdin.write(block)
                self._analyse(np.frombuffer(block, dtype="<f4").reshape(-1, CHANNELS))
                self._started.set()
        except (BrokenPipeError, ValueError) as e:
            self.error = f"Recording interrupted: {e}"
        except Exception as e:  # pragma: no cover - just in case
            self.error = f"Aufnahmefehler: {e}"
        finally:
            self._started.set()

    def _analyse(self, frames: np.ndarray) -> None:
        amp = np.abs(frames).max(axis=1)
        peak = float(amp.max()) if len(amp) else 0.0
        sound = np.flatnonzero(amp > SILENCE_EPS)
        with self.lock:
            base = self.frames
            if len(sound):
                first = base + int(sound[0])
                if self.first_sound is None:
                    self.first_sound = first
                elif first - self.last_sound_end >= GAP_MIN * RATE:
                    self.gaps.append((self.last_sound_end, first))
                self.last_sound_end = base + int(sound[-1]) + 1
            self.frames = base + len(frames)
            self.peak_db = 20 * np.log10(peak) if peak > 1e-7 else -120.0
        if self.on_level:
            self.on_level(self.peak_db)


# ---------------------------------------------------------------------------
# Continuous recording for Auto Record
# ---------------------------------------------------------------------------

ENV_BLOCK = RATE // 100  # envelope in 10 ms blocks


def _flac_writer(path: Path) -> subprocess.Popen:
    return subprocess.Popen(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "f32le", "-ar", str(RATE), "-ac", str(CHANNELS), "-i", "pipe:0",
            "-c:a", "flac", "-sample_fmt", "s32", "-bits_per_raw_sample", "24",
            "-compression_level", "3", "-f", "flac", str(path),
        ],
        stdin=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


class Segment:
    """A piece of the continuous recording (one track plus margin) as a FLAC file.

    `first_frame` is the absolute frame number of the file's first sample;
    `envelope` holds the peak of every 10 ms block for fine cutting.
    """

    def __init__(self, path: Path, first_frame: int):
        self.path = path
        self.first_frame = first_frame
        self.end_frame: int | None = None
        self.frames = 0
        self.envelope = array("f")
        self._rest = np.empty(0, dtype=np.float32)
        self.done = threading.Event()
        self.error: str | None = None
        self._enc = _flac_writer(path)

    def write(self, frames: np.ndarray) -> None:
        if not len(frames) or self.done.is_set():
            return
        try:
            self._enc.stdin.write(frames.tobytes())
        except (BrokenPipeError, ValueError) as e:
            self.error = f"FLAC-Encoder: {e}"
            return
        amp = np.concatenate([self._rest, np.abs(frames).max(axis=1)])
        n = len(amp) // ENV_BLOCK
        if n:
            self.envelope.extend(amp[: n * ENV_BLOCK].reshape(n, ENV_BLOCK).max(axis=1).tolist())
        self._rest = amp[n * ENV_BLOCK :]
        self.frames += len(frames)

    def close(self) -> None:
        if self.done.is_set():
            return
        if self._rest.size:
            self.envelope.append(float(self._rest.max()))
            self._rest = np.empty(0, dtype=np.float32)
        try:
            self._enc.stdin.close()
        except (BrokenPipeError, ValueError):
            pass
        try:
            self._enc.wait(30)
        except subprocess.TimeoutExpired:
            self._enc.kill()
        if self._enc.returncode not in (0, None) and not self.error:
            err = (self._enc.stderr.read() if self._enc.stderr else b"").decode(errors="replace")
            self.error = f"FLAC-Encoder: {err.strip()[:300]}"
        self.done.set()

    def discard(self) -> None:
        self.close()
        self.path.unlink(missing_ok=True)


class StreamCapture:
    """Records the recording output without interruption.

    A ring buffer keeps the last few seconds in memory, so a new track can be
    written to its own file complete from its start (plus margin) even when the
    track change is only noticed a moment later.
    """

    def __init__(self, sink_name: str, workdir: Path, on_level=None, preroll: float = 6.0):
        self.sink_name = sink_name
        self.workdir = workdir
        self.on_level = on_level
        self.preroll = int(preroll * RATE)
        self.lock = threading.Lock()
        self.frames = 0
        self.error: str | None = None
        self._ring: deque[tuple[int, np.ndarray]] = deque()
        self._ring_frames = 0
        self._segments: list[Segment] = []
        self._rec: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._started = threading.Event()

    def start(self, timeout: float = 5.0) -> None:
        if not shutil.which("pw-record") or not shutil.which("ffmpeg"):
            raise ReelcastError("pw-record or ffmpeg is missing.")
        self.workdir.mkdir(parents=True, exist_ok=True)
        self._rec = subprocess.Popen(
            [
                "pw-record", "--target", self.sink_name,
                "-P", "stream.capture.sink=true",
                "-P", "node.description=Reelcast",
                "--rate", str(RATE), "--channels", str(CHANNELS), "--format", "f32",
                "--latency", "50ms", "--raw", "-",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self._thread = threading.Thread(target=self._pump, name="stream-capture", daemon=True)
        self._thread.start()
        if not self._started.wait(timeout):
            self.stop()
            raise ReelcastError("Recording doesn't start (pw-record delivers no data).")

    def stop(self) -> None:
        if self._rec and self._rec.poll() is None:
            self._rec.terminate()
            try:
                self._rec.wait(3)
            except subprocess.TimeoutExpired:
                self._rec.kill()
        if self._thread:
            self._thread.join(5)
        with self.lock:
            segs, self._segments = self._segments, []
        for seg in segs:
            seg.close()

    def open_segment(self, start_frame: int, name: str) -> Segment:
        """New file from `start_frame` (may lie up to `preroll` in the past)."""
        with self.lock:
            start = max(int(start_frame), self.frames - self._ring_frames, 0)
            seg = Segment(self.workdir / f"{name}.flac", start)
            for f0, chunk in self._ring:
                if f0 + len(chunk) > start:
                    seg.write(chunk[max(0, start - f0) :])
            self._segments.append(seg)
            return seg

    def end_segment(self, seg: Segment, end_frame: int) -> None:
        """Stop writing at `end_frame`; the file then closes itself."""
        with self.lock:
            seg.end_frame = max(int(end_frame), seg.first_frame)
            if seg.first_frame + seg.frames >= seg.end_frame:
                self._retire(seg)

    def _retire(self, seg: Segment) -> None:
        if seg in self._segments:
            self._segments.remove(seg)
        threading.Thread(target=seg.close, name="segment-close", daemon=True).start()

    def _pump(self) -> None:
        bytes_per_chunk = CHUNK_FRAMES * CHANNELS * 4
        stdout = self._rec.stdout
        buf = b""
        try:
            while True:
                data = stdout.read1(bytes_per_chunk)
                if not data:
                    break
                buf += data
                usable = len(buf) - len(buf) % (CHANNELS * 4)
                if not usable:
                    continue
                block, buf = buf[:usable], buf[usable:]
                frames = np.frombuffer(block, dtype="<f4").reshape(-1, CHANNELS).copy()
                with self.lock:
                    base = self.frames
                    for seg in list(self._segments):
                        n = len(frames)
                        if seg.end_frame is not None:
                            n = max(0, min(n, seg.end_frame - base))
                        seg.write(frames[:n])
                        if seg.end_frame is not None and base + n >= seg.end_frame:
                            self._retire(seg)
                    self._ring.append((base, frames))
                    self._ring_frames += len(frames)
                    while self._ring and self._ring_frames - len(self._ring[0][1]) >= self.preroll:
                        self._ring_frames -= len(self._ring.popleft()[1])
                    self.frames = base + len(frames)
                self._started.set()
                if self.on_level:
                    peak = float(np.abs(frames).max()) if len(frames) else 0.0
                    self.on_level(20 * np.log10(peak) if peak > 1e-7 else -120.0)
        except Exception as e:  # pragma: no cover - just in case
            self.error = f"Aufnahmefehler: {e}"
        finally:
            self._started.set()
