"""Compare wall clock, captured audio and Spotify's position (is the VM's audio clock slow?)."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from reelcast.audio import RATE, StreamCapture  # noqa: E402
from reelcast.backend import CaptureSink, Spotify  # noqa: E402
from reelcast.model import CancelToken  # noqa: E402

STEP, STEPS = (float(sys.argv[1]), int(sys.argv[2])) if len(sys.argv) > 2 else (4.0, 4)

sp = Spotify()
sp.ensure_running(CancelToken(), minimize=True)
with CaptureSink(listen=False) as sink:
    seen = sink.on_source_event if hasattr(sink, "on_source_event") else None
    if seen:
        def show(event, _seen=seen):
            if event.get("event") == "stats" and event.get("framesFilled"):
                print(f"   helper filled {event['framesFilled'] / RATE * 1000:.0f} ms of dropped audio", flush=True)
            return _seen(event)
        sink.on_source_event = show
    sp.open_uri("spotify:track:0yNttAVwMr39qyODHNIkrY")
    time.sleep(2)
    cap = StreamCapture(sink, Path("/tmp/reelcast-check"))
    cap.start(timeout=15)
    time.sleep(2)
    t0, f0, p0 = time.monotonic(), cap.frames, sp.position()
    for _ in range(STEPS):
        time.sleep(STEP)
        t, f, p = time.monotonic(), cap.frames, sp.position()
        wall, audio, pos = t - t0, (f - f0) / RATE, p - p0
        print(f"wall {wall:7.3f} s | audio {audio:7.3f} s | spotify position {pos:7.3f} s | "
              f"audio-wall {(audio - wall) * 1000:+5.0f} ms ({(audio / wall - 1) * 100:+.2f} %) | "
              f"audio-position {(audio - pos) * 1000:+5.0f} ms", flush=True)
    sp.pause()
    cap.stop()
