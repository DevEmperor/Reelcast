"""Single-episode recording: Spotify plays the episode, Reelcast records exactly that stream.

Ablauf
  1. Start Spotify (minimized, if needed) and open the episode via MPRIS.
  2. Pause right away, move Spotify's stream to the virtual output, set volume
     to 100 % (= unaltered signal), jump to the start.
  3. Start recording, then playback. A watcher keeps Spotify on the recording
     output for the whole episode and detects the end, pauses and failures.
  4. Clean up: stream and volume back, remove the virtual output.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from .audio import RATE, Capture, CaptureSink
from .model import (
    PHASE_CONNECT,
    PHASE_OPEN,
    PHASE_RECORDING,
    PHASE_START_SPOTIFY,
    CancelToken,
    Episode,
    Options,
    ReelcastError,
    Reporter,
)
from .player import Spotify

TICK = 0.2
END_TOLERANCE = 3.0  # a pause this close to the end counts as "episode finished"
NO_SOUND_TIMEOUT = 25.0
SPEED_WINDOW = 2.5  # seconds of recorded audio per speed measurement
SPEED_TOL = 0.04


class SpeedError(ReelcastError):
    """Spotify isn't playing at 1× (podcast speed)."""

    def __init__(self, speed: float):
        self.speed = speed
        shown = f"{speed:.2f}".rstrip("0").rstrip(".")
        super().__init__(
            f"Spotify plays this episode at {shown}× instead of 1× – the recording would be sped up. "
            f"Set the podcast speed in Spotify to 1× (playback bar, \"{shown}x\" button) and try again."
        )


@dataclass
class Recording:
    flac: Path
    segments: list[tuple[int, int]]
    expected: float
    content: float


def _open_episode(sp: Spotify, ep: Episode, cancel: CancelToken, timeout: float = 40) -> dict:
    """Open the episode and wait until Spotify has loaded it."""
    deadline = time.monotonic() + timeout
    next_try = 0.0
    while True:
        now = time.monotonic()
        meta = sp.metadata()
        if Spotify.matches(meta, ep.id) and sp.status() in ("Playing", "Paused"):
            return meta
        if now > deadline:
            raise ReelcastError(
                "Spotify doesn't load the episode. Are you logged in to Spotify and online?"
            )
        if now >= next_try:
            try:
                sp.open_uri(ep.uri)
            except Exception:
                pass
            next_try = now + 4.0
        cancel.sleep(0.25)


def _wait_status(sp: Spotify, wanted: set[str], cancel: CancelToken, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if sp.status() in wanted:
            return True
        cancel.sleep(0.1)
    return False


def record(
    sp: Spotify,
    ep: Episode,
    opts: Options,
    reporter: Reporter,
    cancel: CancelToken,
    flac_path: Path,
) -> Recording:
    reporter.phase(PHASE_START_SPOTIFY if not sp.running() else PHASE_CONNECT)
    sp.ensure_running(cancel, minimize=not opts.show_spotify)

    if sp.status() == "Playing" and not Spotify.matches(sp.metadata(), ep.id) and not opts.force:
        raise ReelcastError(
            "Spotify is playing something else right now. Pause it first – or use --force to interrupt."
        )

    old_volume = sp.volume()
    muted = old_volume is not None and sp.set_volume(0.0)  # no sound on the speakers while loading
    cap: Capture | None = None
    try:
        with CaptureSink(listen=opts.listen) as sink:
            sink.route()  # an already existing (paused) Spotify stream
            reporter.phase(PHASE_OPEN)
            meta = _open_episode(sp, ep, cancel)
            sp.pause()
            _wait_status(sp, {"Paused", "Stopped"}, cancel)
            trackid = str(meta.get("mpris:trackid", ""))
            expected = (meta.get("mpris:length") or 0) / 1e6 or ep.duration
            if not expected:
                raise ReelcastError("Spotify reports no length for this episode.")

            sink.route()
            if muted:
                sp.set_volume(1.0)
            sp.set_position(trackid, 0)
            cancel.sleep(0.3)

            cap = Capture(sink.name, flac_path, on_level=reporter.level)
            cap.start()
            sp.play()
            if not _wait_status(sp, {"Playing"}, cancel, timeout=8):
                raise ReelcastError("Spotify doesn't start playback.")
            sp.release_window()
            reporter.phase(PHASE_RECORDING)
            _watch(sp, sink, cap, ep, trackid, expected, opts, reporter, cancel)
            time.sleep(0.4)  # let the last buffers pass through
            cap.stop()
            sp.pause()
            if cap.error:
                raise ReelcastError(cap.error)
            segs = cap.segments(max_seconds=expected + 0.25)
            content = sum(b - a for a, b in segs) / RATE
            if not segs:
                raise ReelcastError("The recording contains no sound.")
            return Recording(flac_path, segs, expected, content)
    finally:
        if cap:
            cap.stop()
        try:
            if sp.running():
                sp.pause()
                if muted and old_volume is not None:
                    sp.set_volume(old_volume)
        except Exception:
            pass


def _watch(sp, sink, cap, ep, trackid, expected, opts, reporter, cancel) -> None:
    started = time.monotonic()
    reseeked = False
    position_checked = False
    paused_since: float | None = None
    warned_pause = warned_silence = False
    # Spotify doesn't report its podcast speed (the MPRIS rate stays 1.0), so it's measured:
    # progress of Spotify's position divided by progress of the recorded audio.
    # Both stand still while buffering, so the ratio stays meaningful then too.
    speed_ref: tuple[float, float] | None = None  # (Position, Inhalt)
    speed_strikes = 0

    while True:
        cancel.sleep(TICK)
        if cap.error:
            raise ReelcastError(cap.error)
        now = time.monotonic()
        meta = sp.metadata()
        status = sp.status()
        n_streams, newly = sink.route()
        content = cap.content_seconds
        reporter.progress(min(content, expected), expected)

        if newly:
            if content < 5 and not reseeked:
                # Spotify only created its stream on play → the start is missing; start over
                cap.mark_restart()
                sp.set_position(trackid, 0)
                reseeked = True
                speed_ref = None
                continue
            reporter.warn("Spotify rebuilt its audio stream – routed it again.")

        if not position_checked and now - started > 2.0 and status == "Playing":
            position_checked = True
            pos = sp.position()
            if pos is not None and pos > content + 6 and not reseeked:
                cap.mark_restart()
                sp.set_position(trackid, 0)
                reseeked = True
                speed_ref = None
                continue

        if status == "Playing" and cap.first_sound is not None:
            pos = sp.position()
            if pos is None:
                pass
            elif speed_ref is None:
                speed_ref = (pos, content)
            elif content - speed_ref[1] >= SPEED_WINDOW:
                speed = (pos - speed_ref[0]) / (content - speed_ref[1])
                speed_ref = (pos, content)
                if abs(speed - 1.0) <= SPEED_TOL:
                    speed_strikes = 0
                elif not 0.3 < speed < 4.0:
                    sp.pause()
                    raise ReelcastError("The position in Spotify was changed during recording.")
                else:
                    speed_strikes += 1
                    if speed_strikes >= 2:  # twice in a row = not a measurement glitch
                        sp.pause()
                        raise SpeedError(round(speed * 20) / 20)
        elif status != "Playing":
            speed_ref = None

        if not Spotify.matches(meta, ep.id):
            sp.pause()  # next episode/autoplay: stop right away
            if content < expected - END_TOLERANCE:
                raise ReelcastError(
                    "Spotify switched to something else too early "
                    f"(after {content:.0f} of {expected:.0f} s)."
                )
            return
        if content >= expected + 2:
            sp.pause()
            return
        if status == "Stopped":
            if content < expected - END_TOLERANCE:
                raise ReelcastError("Spotify stopped playback before the episode was over.")
            return
        if status == "Paused":
            if content >= expected - END_TOLERANCE:
                return
            paused_since = paused_since or now
            if now - paused_since > 2 and not warned_pause:
                warned_pause = True
                reporter.warn("Spotify was paused – recording waits (the gap is removed later).")
            continue
        if paused_since:
            paused_since = None
            warned_pause = False

        # Spielt, aber es kommt nichts an?
        if cap.first_sound is None and now - started > NO_SOUND_TIMEOUT:
            if n_streams == 0:
                raise ReelcastError(
                    "Spotify is playing, but not on this computer – probably another device is "
                    "active via Spotify Connect. Pick \"This computer\" in Spotify."
                )
            raise ReelcastError("Spotify is playing, but no sound arrives.")
        if cap.first_sound is not None and cap.silent_for > 20 and not warned_silence:
            warned_silence = True
            reporter.warn("Only silence for 20 s – is Spotify buffering? Recording continues.")
        elif cap.silent_for < 1:
            warned_silence = False
