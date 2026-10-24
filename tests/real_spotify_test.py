"""End-to-end test with the real Spotify app (Linux and macOS).

    python3 tests/real_spotify_test.py <report dir> [--listen] [--keep-audio]

The scenario lives in reelcast/selftest.py so that Reelcast.app can run it too
(`Reelcast --selftest DIR`).
"""

import os
import sys
from pathlib import Path

if sys.platform != "darwin":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # never show the test window on Linux
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reelcast.selftest import run  # noqa: E402

sys.exit(run(Path(sys.argv[1]), listen="--listen" in sys.argv, keep_audio="--keep-audio" in sys.argv))
