"""Entry point of Reelcast.app (PyInstaller).

Without arguments the window opens. `--selftest DIR` runs the end-to-end test with
the real Spotify app and writes a report – the same test the repository runs.
"""

import sys

from reelcast.cli import main

if __name__ == "__main__":
    args = sys.argv[1:]
    # Finder may pass -psn_… on older systems; anything unknown means "just open the window"
    if not args or args[0].startswith("-psn"):
        args = ["--gui"]
    sys.exit(main(args))
