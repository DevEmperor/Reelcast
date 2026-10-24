"""Sample Spotify's position every 0.1 s, as the session does, and show where it jumps.

Plays a track, pauses it for 4 s, skips to the next one. Prints every sample whose
position step differs from the wall-clock step by more than 0.25 s, track changes,
status changes – and a summary at the end.
"""

import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from reelcast.backend import Spotify  # noqa: E402
from reelcast.model import CancelToken  # noqa: E402

TRACK = sys.argv[1] if len(sys.argv) > 1 else "spotify:track:0yNttAVwMr39qyODHNIkrY"
raw_reader = None
if sys.platform == "darwin":
    from reelcast.macbackend import _StateReader

    raw_reader = _StateReader.get

sp = Spotify()
sp.ensure_running(CancelToken(), minimize=True)
sp.open_uri(TRACK)
t0 = time.monotonic()
last = None  # (wall, pos, tid, status)
diffs = []
spans = []
actions = {10.0: "pause", 14.0: "play", 25.0: "next"}
done = set()
while (now := time.monotonic()) - t0 < 35:
    for at, what in actions.items():
        if now - t0 >= at and at not in done:
            done.add(at)
            print(f"{now - t0:6.2f}  >>> {what}", flush=True)
            {"pause": sp.pause, "play": sp.play, "next": sp.next_track}[what]()
    now = time.monotonic()
    status, meta, pos = sp.snapshot()
    tid = str(meta.get("mpris:trackid", ""))[-22:]
    raw = raw_reader().state if raw_reader else {}
    note = ""
    if last and pos is not None and last[1] is not None:
        dt, dpos = now - last[0], pos - last[1]
        if tid != last[2]:
            note = f"track {last[2]} -> {tid}"
        elif status != last[3]:
            note = f"status {last[3]} -> {status}"
        elif status == "Playing" and last[3] == "Playing":
            diffs.append(dpos - dt)
            if abs(dpos - dt) > 0.25:
                note = f"JUMP dpos-dt {dpos - dt:+.3f}"
        if note:
            age = now - raw.get("_mono", now)
            print(f"{now - t0:6.2f}  {status:7} pos {pos:8.3f}  raw {raw.get('position', 0):8.3f} "
                  f"age {age:5.3f}  span {raw.get('posSpan', 0):5.3f}  {note}", flush=True)
    last = (now, pos, tid, status)
    spans.append(raw.get("posSpan", 0.0))
    time.sleep(0.1)
sp.pause()
if diffs:
    print(f"samples {len(diffs)}  dpos-dt median {statistics.median(diffs):+.4f}  "
          f"min {min(diffs):+.3f}  max {max(diffs):+.3f}  "
          f">0.25: {sum(abs(d) > 0.25 for d in diffs)}  >0.5: {sum(abs(d) > 0.5 for d in diffs)}")
    print(f"position query: median {statistics.median(spans) * 1000:.0f} ms, max {max(spans) * 1000:.0f} ms")
