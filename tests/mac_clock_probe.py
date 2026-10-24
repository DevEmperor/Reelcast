"""Compare wall clock, captured audio and Spotify's position (is the VM's audio clock slow?)."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from reelcast.audio import RATE, StreamCapture  # noqa: E402
from reelcast.backend import CaptureSink, Spotify  # noqa: E402
from reelcast.model import CancelToken  # noqa: E402

sp = Spotify()
sp.ensure_running(CancelToken(), minimize=True)
with CaptureSink(listen=False) as sink:
    sp.open_uri("spotify:track:0yNttAVwMr39qyODHNIkrY")
    time.sleep(2)
    cap = StreamCapture(sink, Path("/tmp/reelcast-check"))
    cap.start(timeout=15)
    time.sleep(2)
    t0, f0, p0 = time.monotonic(), cap.frames, sp.position()
    for _ in range(4):
        time.sleep(4)
        t, f, p = time.monotonic(), cap.frames, sp.position()
        print(f"wall {t - t0:5.1f} s | audio {(f - f0) / RATE:5.1f} s | spotify position {p - p0:5.1f} s", flush=True)
    sp.pause()
    cap.stop()
