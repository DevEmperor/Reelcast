"""Quick macOS check: control Spotify and record 8 s of its audio through the helper."""

import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reelcast.audio import RATE, StreamCapture  # noqa: E402
from reelcast.backend import CaptureSink, Spotify  # noqa: E402
from reelcast.model import CancelToken  # noqa: E402

levels = []
sp = Spotify()
sp.ensure_running(CancelToken(), minimize=True)
print("spotify running:", sp.running(), "error:", sp.last_error or "-", flush=True)
with CaptureSink(listen=False) as sink:
    cap = StreamCapture(sink, Path("/tmp/reelcast-check"), on_level=levels.append)
    cap.start(timeout=15)
    sp.open_uri("spotify:track:0yNttAVwMr39qyODHNIkrY")
    time.sleep(8)
    status, meta, pos = sp.snapshot()
    print("status:", status, "| title:", meta.get("xesam:title"), "| position:", round(pos or 0, 1), flush=True)
    sp.pause()
    time.sleep(0.5)
    print("frames captured:", cap.frames, f"({cap.frames / RATE:.1f} s)", "| tap processes:", sink.route()[0], flush=True)
    print("peak dBFS (max):", round(max(levels), 1) if levels else None, "| error:", cap.error, flush=True)
    cap.stop()
print("CHECK", "OK" if levels and max(levels) > -60 else "NO AUDIO", flush=True)
