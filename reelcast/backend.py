"""Platform backend: PipeWire + MPRIS on Linux, process tap + AppleScript on macOS.

Both expose the same two classes:

* `CaptureSink` – makes Spotify's audio recordable and tells the capture where the
  PCM comes from (`source_command`).
* `Spotify` – reads Spotify's state and controls playback.
"""

from __future__ import annotations

from .tools import IS_MAC

if IS_MAC:
    from .macbackend import MacSink as CaptureSink, MacSpotify as Spotify  # noqa: F401
else:
    from .audio import CaptureSink  # noqa: F401
    from .player import Spotify  # noqa: F401
