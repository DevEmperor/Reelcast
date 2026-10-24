"""Run the macOS helper's tap for 12 s while Spotify plays; print its events and the PCM it delivers."""

import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from reelcast import tools  # noqa: E402

H = tools.helper()
print(subprocess.run([H, "check"], capture_output=True, text=True).stdout, flush=True)
tap = subprocess.Popen([H, "tap"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
data = bytearray()
threading.Thread(target=lambda: [data.extend(c) for c in iter(lambda: tap.stdout.read1(65536), b"")], daemon=True).start()
threading.Thread(target=lambda: [print("helper:", l.decode().strip(), flush=True) for l in tap.stderr], daemon=True).start()
time.sleep(1)
print("open:", subprocess.run([H, "control", "open", "spotify:track:0yNttAVwMr39qyODHNIkrY"], capture_output=True, text=True).stderr or "ok", flush=True)
time.sleep(12)
subprocess.run([H, "control", "pause"])
tap.terminate()
time.sleep(1)
a = np.frombuffer(bytes(data[: len(data) // 8 * 8]), dtype="<f4")
print(f"PCM: {len(a) // 2 / 48000:.2f} s, peak {float(np.abs(a).max()) if len(a) else 0:.4f}", flush=True)
