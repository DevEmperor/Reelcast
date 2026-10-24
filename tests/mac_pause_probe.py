"""Pause and resume Spotify many times while sampling its position like the session does.

Counts samples in which Spotify still says "Playing" but the position went backwards
(what the session would take for a seek). Commands run in the background, so sampling
goes on while Spotify switches.
"""

import random
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from reelcast.backend import Spotify  # noqa: E402
from reelcast.model import CancelToken  # noqa: E402

CYCLES = int(sys.argv[1]) if len(sys.argv) > 1 else 12
sp = Spotify()
sp.ensure_running(CancelToken(), minimize=True)
sp.open_uri("spotify:track:0txA8cFKpWRm9qjuetgH3O", "spotify:album:4C4LvbYS0pxXLW5sGD9EK5")
time.sleep(3)

events, at = [], 0.0
for i in range(CYCLES * 2):
    at += random.uniform(2.0, 4.0)
    events.append((at, "pause" if i % 2 == 0 else "play"))
end = events[-1][0] + 3
start = time.monotonic()
last = None
samples = backwards = 0
while (now := time.monotonic()) - start < end:
    while events and now - start >= events[0][0]:
        _, what = events.pop(0)
        threading.Thread(target=sp.pause if what == "pause" else sp.play, daemon=True).start()
        print(f"{now - start:6.2f}  >>> {what}", flush=True)
    status, _meta, pos = sp.snapshot()
    if status == "Playing" and last and last[0] == "Playing" and pos is not None:
        samples += 1
        drift = (pos - last[1]) - (now - last[2])
        if drift < -0.3:
            backwards += 1
            print(f"{now - start:6.2f}  BACKWARDS {drift:+.2f} s while Playing", flush=True)
    last = (status, pos, now)
    time.sleep(0.1)
sp.pause()
print(f"{CYCLES} pause/play cycles, {samples} playing samples, {backwards} backward jumps")
