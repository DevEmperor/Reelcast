"""PipeWire-Seite der Aufnahme.

* `CaptureSink` legt einen virtuellen Ausgang an, leitet *nur* den Spotify-Strom
  dorthin um und räumt danach alles wieder auf (Strom zurück, Lautstärke zurück,
  Modul entladen). Systemklänge und andere Programme landen nie in der Aufnahme.
* `Capture` liest den Monitor dieses Ausgangs verlustfrei (float32, 48 kHz),
  misst Pegel und digitale Stille live und schreibt parallel eine FLAC-Datei.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .model import ReelcastError

RATE = 48000  # Takt des PipeWire-Graphen; so wird nur einmal (von Spotify aus) umgerechnet
CHANNELS = 2
CHUNK_FRAMES = RATE // 10  # 100 ms
SILENCE_EPS = 1e-6  # ≈ −120 dBFS: nur echte digitale Stille (Pause, Puffern)
GAP_MIN = 1.0  # so lange digitale Stille gilt als Lücke und wird entfernt

SINK_NAME = "reelcast"
APP_MATCH = os.environ.get("REELCAST_APP_MATCH", "spotify").lower()


def _run(*args: str, check: bool = True) -> str:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=15)
    except FileNotFoundError as e:
        raise ReelcastError(f"Programm fehlt: {args[0]}") from e
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
    """Gespeichertes Ziel (target.object) eines Stroms, None = folgt dem Standardausgang."""
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

    # ---- Auf- und Abbau -------------------------------------------------

    def __enter__(self) -> "CaptureSink":
        self.remove_stale()
        mod = _run(
            "pactl",
            "load-module",
            "module-null-sink",
            f"sink_name={self.name}",
            "sink_properties=device.description=Reelcast-Aufnahme",
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
        self.restore()
        for mod in reversed(self.modules):
            _run("pactl", "unload-module", mod, check=False)
        self.modules.clear()

    def remove_stale(self) -> None:
        """Überbleibsel eines abgestürzten Laufs entfernen."""
        for m in pactl_json("list", "modules"):
            arg = m.get("argument") or ""
            if m.get("name") in ("module-null-sink", "module-loopback") and (
                f"sink_name={self.name}" in arg or f"source={self.name}.monitor" in arg
            ):
                _run("pactl", "unload-module", str(m.get("index")), check=False)

    # ---- Spotify-Strom ----------------------------------------------------

    def _sinks(self) -> dict[int, str]:
        return {s["index"]: s["name"] for s in pactl_json("list", "sinks")}

    def app_streams(self) -> list[dict]:
        return [si for si in pactl_json("list", "sink-inputs") if _is_app_stream(si)]

    def route(self) -> tuple[int, int]:
        """Alle Spotify-Ströme auf den Aufnahme-Ausgang legen.

        Gibt (Anzahl Ströme, davon neu umgeleitet) zurück.
        """
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

    def restore(self) -> None:
        """Ströme zurück an ihren alten Platz, alte Lautstärke wieder herstellen."""
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
                # Der Strom folgte vorher dem Standardausgang: feste Bindung wieder lösen
                _run("pw-metadata", "-n", "default", "-d", mv.node_id, "target.object", check=False)
            _run("pactl", "set-sink-input-volume", str(idx), *map(str, mv.orig_volume), check=False)
            _run("pactl", "set-sink-input-mute", str(idx), "1" if mv.orig_mute else "0", check=False)
        self.moved.clear()


class Capture:
    """Nimmt den Monitor des Aufnahme-Ausgangs auf und analysiert live."""

    def __init__(self, sink_name: str, flac_path: Path, on_level=None):
        self.sink_name = sink_name
        self.flac_path = flac_path
        self.on_level = on_level
        self.lock = threading.Lock()
        self.frames = 0  # insgesamt geschriebene Frames
        self.cut_before = 0  # alles davor wird verworfen
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
                raise ReelcastError(f"Programm fehlt: {tool}")
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
            raise ReelcastError("Aufnahme startet nicht (pw-record liefert keine Daten).")

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
        """Alles bisher Aufgenommene verwerfen (z. B. nach erneutem Sprung an den Anfang)."""
        with self.lock:
            self.cut_before = self.frames
            self.first_sound = None
            self.last_sound_end = self.frames
            self.gaps = []

    # ---- Auswertung -------------------------------------------------------

    @property
    def content_seconds(self) -> float:
        """Hörbare Länge seit dem ersten Ton, ohne Lücken."""
        with self.lock:
            if self.first_sound is None:
                return 0.0
            gaps = sum(b - a for a, b in self.gaps)
            return (self.last_sound_end - self.first_sound - gaps) / RATE

    @property
    def silent_for(self) -> float:
        """Sekunden digitaler Stille am Ende der bisherigen Aufnahme."""
        with self.lock:
            return (self.frames - max(self.last_sound_end, self.cut_before)) / RATE

    def segments(self, max_seconds: float | None = None) -> list[tuple[int, int]]:
        """Zu behaltende Abschnitte (Frame-Bereiche) der FLAC-Datei."""
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
            self.error = f"Aufnahme abgebrochen: {e}"
        except Exception as e:  # pragma: no cover - nur zur Sicherheit
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
