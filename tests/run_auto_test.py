"""End-to-end test of Auto Record against the fake Spotify player.

Checks: number of saved tracks, length within ±30 ms, no bleed from neighbouring
tracks at the edges, pause removed, intended silence kept, ad and skipped track
discarded. None of it is audible (the player plays into a silent helper output).
"""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

os.environ["REELCAST_MPRIS_NAME"] = "org.mpris.MediaPlayer2.reelcasttest"
os.environ["REELCAST_APP_MATCH"] = "reelcastfake"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from reelcast import paths  # noqa: E402
from reelcast.model import CancelToken, Options  # noqa: E402
from reelcast.session import AutoSession, SessionReporter  # noqa: E402

work = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/reelcast-test")
fmt = sys.argv[2] if len(sys.argv) > 2 else "flac"
out = work / "out"
subprocess.run(["rm", "-rf", str(work)])
work.mkdir(parents=True)


class Rep(SessionReporter):
    def status(self, text):
        print(f"[{time.monotonic():8.2f}] STATUS {text}")

    def entry(self, e):
        print(f"[{time.monotonic():8.2f}] ENTRY {e.key} {e.kind:7} {e.state:9} {e.title} {e.reason} {e.path or ''}")

    def info(self, text):
        print("INFO", text)

    def warn(self, text):
        print("WARN", text)


dummy = subprocess.run(
    ["pactl", "load-module", "module-null-sink", "sink_name=reelcast_test_dummy"],
    capture_output=True, text=True,
).stdout.strip()
try:
    cancel = CancelToken()
    session = AutoSession(Options(out_dir=out, fmt=fmt, listen=False), Rep(), cancel)
    th = threading.Thread(target=session.run)
    th.start()
    time.sleep(1.0)
    fake = subprocess.Popen(
        [sys.executable, str(Path(__file__).with_name("fake_spotify.py")), str(work)],
        env={**os.environ, "FAKE_TARGET": "reelcast_test_dummy"},
    )
    fake.wait()
    time.sleep(1.0)
    cancel.cancel()
    th.join(30)
finally:
    subprocess.run(["pactl", "unload-module", dummy])

expected = json.loads((work / "expected.json").read_text())
saved = [e for e in session.entries if e.state == "saved"]
print("\nSaved:", len(saved), "expected:", len(expected))
ok = len(saved) == len(expected)


def decode(path):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "1", "-ar", "48000", "-"],
                         capture_output=True).stdout
    return np.frombuffer(raw, dtype="<f4")


def freq(x):
    if len(x) < 256 or np.abs(x).max() < 1e-3:
        return 0
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    return round(np.argmax(spec) * 48000 / len(x))


for exp in expected:
    p = paths.track_path(out, "Test Band", "Test Album", exp["title"], exp["number"], 0, fmt)
    if not p.exists():
        print("MISSING", p.name)
        ok = False
        continue
    x = decode(p)
    dur = len(x) / 48000
    head = x[int(0.01 * 48000):int(0.11 * 48000)]
    sound = np.flatnonzero(np.abs(x) > 1e-3)
    tail = x[sound[-1] - int(0.1 * 48000):sound[-1]] if len(sound) else x[:0]
    zeros = np.abs(x) < 1e-6
    longest = 0
    run = 0
    for z in zeros[::48]:  # 1 ms grid
        run = run + 1 if z else 0
        longest = max(longest, run)
    fh, ft = freq(head), freq(tail)
    good = abs(dur - exp["length"]) <= 0.03 and abs(fh - exp["freq"]) <= 15 and abs(ft - exp["freq"]) <= 15
    print(f"{'OK ' if good else 'XX '} {p.name:28} {dur:7.3f}s (want {exp['length']:.3f})  "
          f"start {fh} Hz, end {ft} Hz (want {exp['freq']}), longest silence {longest / 1000:.2f}s")
    ok &= good
print("\nRESULT:", "PASSED" if ok else "FAILED")
print("Files:", sorted(str(q.relative_to(out)) for q in out.rglob("*") if q.is_file()))
