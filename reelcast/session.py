"""Auto Record: record everything Spotify plays on this computer, track by track.

Reelcast keeps Spotify on the recording output and records without a break.
On every track change the continuous recording is split:

* **Boundaries** come from Spotify's position: recording frame minus position ×
  sample rate gives where in the stream the track was at 0:00. The median of many
  measurements is accurate to a few milliseconds. If there's silence at the
  boundary, the cut goes exactly there. Measured offsets calibrate the estimate
  for gapless transitions.
* **Pauses and buffering** are detected by Spotify's position standing still while
  the recording keeps running. Only those spots are cut out; intended silence in
  music stays.
* **Saved** is only what played completely: from 0:00 to the end, no seeking,
  at 1× speed. Ads and local files are ignored.
"""

from __future__ import annotations

import itertools
import os
import queue
import sys
import statistics
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import artwork, doctor, encode, paths, __version__
from .audio import ENV_BLOCK, RATE, Segment, StreamCapture
from .backend import CaptureSink, Spotify
from .model import CancelToken, Cancelled, Options, Reporter, fmt_duration
from .spotify_meta import fetch_episode, fetch_track_release

TICK = 0.1
ROUTE_EVERY = 0.5  # seconds between routing/volume checks
MARGIN = 2.0  # margin before and after each track in the raw file
SEARCH = 0.6  # search window for cutting in silence
START_TOL = 1.5  # a track first seen this late still counts as "from the start"
END_TOL = 2.5  # the last seen position must be this close to the end
SPEED_TOL = 0.04
DEBUG = bool(os.environ.get("REELCAST_DEBUG"))
QUIET = 10 ** (-85 / 20)  # silence for fine cutting (practically digital zero)
ZERO = 1e-6  # digital zero (pause/buffering)
MIN_STALL = 0.2  # a standing position counts as buffering from this long (and is cut from it)
SOUND = 10 ** (-60 / 20)  # a track whose loudest moment stays below this was not recorded

# Session states for front ends – the display text is up to them.
WAITING = "waiting"  # Spotify isn't running
READY = "ready"  # nothing is playing
RECORDING = "recording"
PAUSED = "paused"
IGNORED = "ignored"  # something plays that isn't recorded (ad, local file)


@dataclass
class Entry:
    """An entry in the session log (for display)."""

    key: int
    kind: str  # track | episode | ad | other
    title: str
    subtitle: str
    length: float
    state: str = "recording"  # recording | partial | encoding | saved | exists | skipped | failed
    reason: str = ""
    path: Path | None = None
    duration: float = 0.0
    artist: str = ""
    album: str = ""
    art_url: str = ""


class SessionReporter(Reporter):
    def status(self, text: str) -> None: ...

    def now_playing(self, entry: Entry | None) -> None: ...

    def position(self, pos: float, length: float) -> None: ...

    def entry(self, entry: Entry) -> None:
        """New or changed log entry."""


@dataclass
class _Item:
    entry: Entry
    trackid: str
    meta: dict
    seg: Segment | None
    first_pos: float
    last_pos: float
    last_wall: float
    last_frame: int
    z_runs: list[list[float]] = field(default_factory=lambda: [[]])
    stalls: list[tuple[int, int]] = field(default_factory=list)
    stall_open: int | None = None
    played: float = 0.0
    advanced: float = 0.0
    seeked: bool = False
    speed: float | None = None
    prev_boundary: "_Boundary | None" = None
    late_start: bool = False  # audio reached the recording late – don't trust its start
    warned_silent: bool = False


class _Boundary:
    """Grenze zwischen zwei Titeln, gemessen am Anfang des Folgetitels."""

    def __init__(self) -> None:
        self.z: float | None = None
        self.ready = threading.Event()

    def set(self, z: float | None) -> None:
        if not self.ready.is_set():
            self.z = z
            self.ready.set()


def _kind(trackid: str) -> str:
    for kind in ("track", "episode", "ad"):
        if f"/{kind}/" in trackid or f":{kind}:" in trackid:
            return kind
    return "other"


def _item_id(meta: dict) -> str:
    url = str(meta.get("xesam:url", ""))
    tid = str(meta.get("mpris:trackid", ""))
    return (url.rstrip("/").rsplit("/", 1)[-1] or tid.rsplit("/", 1)[-1]).split("?")[0]


def _join(v) -> str:
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v if x)
    return str(v or "")


class AutoSession:
    def __init__(self, opts: Options, reporter: SessionReporter, cancel: CancelToken | None = None):
        self.opts = opts
        self.rep = reporter
        self.cancel = cancel or CancelToken()
        self.entries: list[Entry] = []
        self._keys = itertools.count(1)
        self._jobs: queue.Queue = queue.Queue()
        # Latency from Spotify's position to the recording: every track boundary with
        # silence yields a lower or upper bound (frames); the delay is estimated from them.
        self._lat_low: list[float] = []
        self._lat_high: list[float] = []
        self._open_boundary: _Boundary | None = None
        self._late_flag = False
        self.workdir = opts.out_dir / ".reelcast-tmp"

    # ---- Ablauf -------------------------------------------------------------

    def run(self) -> None:
        prefs = doctor.spotify_prefs()
        if prefs.get("audio.crossfade_v2") == "true":
            self.rep.warn("Crossfade is on in Spotify – transitions get blended and tracks can't "
                          "be separated cleanly. (Settings → Playback)")
        sp = Spotify()
        worker = threading.Thread(target=self._encoder, name="encoder", daemon=True)
        worker.start()
        try:
            with CaptureSink(listen=self.opts.listen) as sink:
                sink.route()
                sink.watch()
                cap = StreamCapture(sink, self.workdir, on_level=self.rep.level)
                cap.start()
                try:
                    self._loop(sp, sink, cap)
                finally:
                    cap.stop()
        finally:
            self._jobs.put(None)
            worker.join()
            sp.close()
            try:
                self.workdir.rmdir()
            except OSError:
                pass

    def _loop(self, sp: Spotify, sink: CaptureSink, cap: StreamCapture) -> None:
        item: _Item | None = None
        shown = ""
        next_route = 0.0
        warned_volume = False
        warned_problem = ""
        n_streams = 1

        def show(text: str) -> None:
            nonlocal shown
            if text != shown:
                shown = text
                self.rep.status(text)

        try:
            while True:
                self.cancel.sleep(TICK)
                if cap.error:
                    self.rep.warn(cap.error)
                    return
                now = time.monotonic()
                if not sp.running():
                    if item:
                        self._finish(item, cap, natural=False, reason="Spotify was closed")
                        item = None
                        self.rep.now_playing(None)
                    show(WAITING)
                    continue
                if now >= next_route:
                    next_route = now + ROUTE_EVERY
                    n_streams, _ = sink.route()
                    if sink.enforce_full_volume() and not warned_volume:
                        warned_volume = True
                        self.rep.info("Spotify stays at 100 % while recording – use your system volume instead.")

                status, meta, pos = sp.snapshot()
                frame = cap.frames
                if sink.take_late_routes() and item and self._restart(sp, item, cap):
                    item = None
                    continue
                tid = str(meta.get("mpris:trackid", ""))
                if status == "Unknown":
                    problem = getattr(sp, "last_error", "")
                    if problem and problem != warned_problem:
                        warned_problem = problem
                        self.rep.warn(problem)
                    continue
                if not tid or status == "Stopped" or pos is None:
                    if item:
                        self._finish(item, cap, natural=self._at_end(item, now, status))
                        item = None
                        self.rep.now_playing(None)
                    show(READY)
                    continue

                # Back to the start = the track starts over. Spotify for Mac also does this on
                # its own right after a track begins (first 1.6 s, then 0.1 s).
                restarted = item is not None and tid == item.trackid and (
                    pos + 2.0 < item.last_pos or (pos < START_TOL and pos + 0.5 < item.last_pos))
                if item is None or tid != item.trackid or restarted:
                    if item:
                        early = restarted and item.played < 3.0
                        carried = None
                        if early:
                            carried, item.prev_boundary = item.prev_boundary, None
                        self._finish(item, cap, natural=self._at_end(item, now, "Playing"))
                        if early:  # a few seconds that start over: no entry, and the transition
                            self._drop(item.entry)  # from the previous track is measured anew
                            self._open_boundary = carried
                    item = self._begin(meta, tid, pos, frame, now, cap)
                    show(RECORDING if item.seg else IGNORED)
                    continue

                self._track(item, status, pos, frame, now)
                self._check_sound(item, status, cap, n_streams)
                show(PAUSED if status == "Paused" else (RECORDING if item.seg else IGNORED))
                self.rep.position(pos, item.entry.length)
        except Cancelled:
            pass
        finally:
            if item:
                self._finish(item, cap, natural=False, reason="recording stopped")
                self.rep.now_playing(None)

    # ---- following tracks -----------------------------------------------------

    def _begin(self, meta: dict, tid: str, pos: float, frame: int, now: float, cap: StreamCapture) -> _Item:
        kind = _kind(tid)
        title = str(meta.get("xesam:title") or "Unknown")
        if kind == "episode":
            subtitle = str(meta.get("xesam:album") or _join(meta.get("xesam:artist")))
        else:
            subtitle = " · ".join(x for x in (_join(meta.get("xesam:artist")), str(meta.get("xesam:album") or "")) if x)
        length = (meta.get("mpris:length") or 0) / 1e6
        entry = Entry(next(self._keys), kind, title, subtitle, length,
                      artist=_join(meta.get("xesam:artist")), album=str(meta.get("xesam:album") or ""),
                      art_url=str(meta.get("mpris:artUrl") or ""))
        seg = None
        if kind == "ad":
            entry.state, entry.reason = "skipped", "ad – ignored"
        elif kind == "other":
            entry.state, entry.reason = "skipped", "local file"
        elif not length:
            entry.state, entry.reason = "skipped", "Spotify reports no length"
        else:
            if pos > START_TOL:
                entry.state, entry.reason = "partial", "not from the start"
            seg = cap.open_segment(int(frame - (pos + MARGIN) * RATE), f"track-{entry.key}")
        item = _Item(entry, tid, meta, seg, first_pos=pos, last_pos=pos, last_wall=now, last_frame=frame)
        item.z_runs[0].append(frame - pos * RATE)
        item.prev_boundary, self._open_boundary = self._open_boundary, None
        item.late_start, self._late_flag = self._late_flag, False
        if item.prev_boundary and pos > START_TOL:
            item.prev_boundary.set(None)  # not a direct transition
        self.entries.append(entry)
        self.rep.entry(entry)
        self.rep.now_playing(entry)
        return item

    def _track(self, item: _Item, status: str, pos: float, frame: int, now: float) -> None:
        dt = now - item.last_wall
        dpos = pos - item.last_pos
        if status == "Playing" and dt > 0:
            if dpos < -0.5 or dpos > dt + 1.0:
                if DEBUG:
                    print(f"[session] seek in {item.entry.title!r}: {item.last_pos:.2f} → {pos:.2f} s "
                          f"within {dt:.2f} s", file=sys.stderr)
                item.seeked = True
                if item.entry.state == "recording":
                    item.entry.state, item.entry.reason = "partial", "seeked"
                    self.rep.entry(item.entry)
            elif dpos < 0.3 * dt:
                if item.stall_open is None:  # buffering: position stands, recording runs
                    item.stall_open = item.last_frame
            else:
                if item.stall_open is not None:
                    # Spotify for Mac now and then reports a standing position for a moment
                    # while it keeps playing – only a longer stand is buffering.
                    if item.last_frame - item.stall_open >= MIN_STALL * RATE:
                        item.stalls.append((item.stall_open, item.last_frame))
                        item.z_runs.append([])
                    item.stall_open = None
                item.played += dt
                item.advanced += dpos
                item.z_runs[-1].append(frame - pos * RATE)
                if item.prev_boundary and len(item.z_runs) == 1 and len(item.z_runs[0]) >= 10:
                    item.prev_boundary.set(statistics.median(item.z_runs[0]))
                if item.speed is None and item.played >= 3.0 and item.entry.kind == "episode":
                    ratio = item.advanced / item.played
                    if abs(ratio - 1.0) > SPEED_TOL:
                        item.speed = ratio
                        shown = f"{round(ratio * 20) / 20:.2f}".rstrip("0").rstrip(".")
                        if item.entry.state == "recording":
                            item.entry.state, item.entry.reason = "partial", f"speed {shown}×"
                            self.rep.entry(item.entry)
                    else:
                        item.speed = 1.0
        elif status == "Paused" and item.stall_open is None:
            item.stall_open = item.last_frame
            if item.prev_boundary:
                item.prev_boundary.set(statistics.median(item.z_runs[0]) if item.z_runs[0] else None)
        item.last_pos, item.last_wall, item.last_frame = pos, now, frame

    def _check_sound(self, item: _Item, status: str, cap: StreamCapture, n_streams: int) -> None:
        """Spotify plays, but nothing arrives – most likely Spotify Connect plays elsewhere."""
        if status != "Playing" or item.seg is None or item.warned_silent:
            return
        if cap.silent_for > 8 and n_streams == 0:
            item.warned_silent = True
            if item.entry.state == "recording":
                item.entry.state, item.entry.reason = "partial", "not playing on this computer"
                self.rep.entry(item.entry)
            self.rep.warn("Spotify is playing, but not on this computer (Spotify Connect). "
                          "Pick “This computer” in Spotify – nothing is recorded until then.")
        elif cap.silent_for > 15:
            item.warned_silent = True
            self.rep.warn("No sound has arrived from Spotify for 15 s – is it playing on another "
                          "device (Spotify Connect)?")

    def _restart(self, sp: Spotify, item: _Item, cap: StreamCapture) -> bool:
        """Spotify's stream arrived only after the track began → start missing: restart once."""
        if item.seg is None or item.entry.state != "recording" or item.last_pos > 3.0:
            return False
        sp.set_position(item.trackid, 0)
        cap.end_segment(item.seg, cap.frames)
        threading.Thread(target=item.seg.discard, daemon=True).start()
        self._late_flag = True
        self._drop(item.entry)
        self.rep.info("Spotify's audio arrived a moment late – jumped back to the start of the track.")
        return True

    def _drop(self, entry: Entry) -> None:
        """Remove an entry from the log altogether."""
        entry.state = "dropped"
        if entry in self.entries:
            self.entries.remove(entry)
        self.rep.entry(entry)

    def _at_end(self, item: _Item, now: float, status: str) -> bool:
        guess = item.last_pos + (now - item.last_wall if status == "Playing" else 0.0)
        return max(item.last_pos, min(guess, item.entry.length)) >= item.entry.length - END_TOL

    def _finish(self, item: _Item, cap: StreamCapture, natural: bool, reason: str = "") -> None:
        if item.prev_boundary:
            run = item.z_runs[0]
            item.prev_boundary.set(statistics.median(run) if run else None)
        boundary = _Boundary() if natural else None
        self._open_boundary = boundary
        if item.seg is None:
            if boundary:
                boundary.set(None)
            return
        if item.stall_open is not None:
            item.stalls.append((item.stall_open, cap.frames))
            item.stall_open = None
        entry = item.entry
        if entry.state == "recording" and not natural:
            entry.state, entry.reason = "partial", reason or "skipped"
        if entry.state != "recording":
            if boundary:
                boundary.set(None)
                self._open_boundary = None
            cap.end_segment(item.seg, cap.frames)
            threading.Thread(target=item.seg.discard, daemon=True).start()
            entry.state = "skipped"
            self.rep.entry(entry)
            return
        head = [z for z in item.z_runs[0]] or [cap.frames - item.last_pos * RATE]
        tail = next((run for run in reversed(item.z_runs) if run), head)
        start = statistics.median(head)
        end = statistics.median(tail) + entry.length * RATE
        # exact length of every pause from the jump of the estimate before/after it
        meds = [statistics.median(r) if r else None for r in item.z_runs]
        stalls = []
        for i, (a, b) in enumerate(item.stalls):
            want = b - a
            if i + 1 < len(meds) and meds[i] is not None and meds[i + 1] is not None:
                jump = meds[i + 1] - meds[i]
                if jump > 0:
                    want = jump
            stalls.append((a, b, want))
        cap.end_segment(item.seg, int(end + MARGIN * RATE))
        entry.state = "encoding"
        self.rep.entry(entry)
        self._jobs.put((item, start, end, stalls, boundary))

    # ---- cutting and saving (own thread) ----------------------------------------

    def _encoder(self) -> None:
        """Measure every finished track at once, but cut only once the latency is known.

        The first boundary of a session is often unreliable (the stream was just being
        routed). Tracks wait – at most a few – until three clean bound measurements
        exist or the session ends, then they're cut with the best estimate.
        """
        pending: list[dict] = []
        closing = False
        while not (closing and not pending):
            try:
                job = self._jobs.get(timeout=0.5)
            except queue.Empty:
                job = False
            if job is None:
                closing = True
            elif job:
                try:
                    pending.append(self._measure(*job))
                except Exception as e:
                    self._fail(job[0], e)
            enough = len(self._lat_low) + len(self._lat_high) >= 3
            while pending and (enough or closing or len(pending) > 3):
                prepared = pending.pop(0)
                try:
                    self._save(prepared)
                except Exception as e:
                    self._fail(prepared["item"], e)

    def _fail(self, item: _Item, error: Exception) -> None:
        item.entry.state, item.entry.reason = "failed", str(error)[:200]
        self.rep.entry(item.entry)
        if item.seg:
            item.seg.path.unlink(missing_ok=True)

    def _measure(self, item: _Item, start: float, end: float, stalls, boundary) -> dict:
        seg = item.seg
        seg.done.wait()
        if boundary:
            boundary.ready.wait(3.0)
            # Gapless transition: the start of the next track is the more precise end
            if boundary.z is not None and abs(boundary.z - end) < 0.5 * RATE:
                # Spotify's position can run ahead of its audio after a skip or a fresh
                # start (it counts while still buffering). The same offset then shows up
                # between this track's own end estimate and the clean boundary – apply it
                # to the start as well.
                start += boundary.z - end
                end = boundary.z
        if seg.error:
            raise RuntimeError(seg.error)
        s_raw, e_raw = start - seg.first_frame, end - seg.first_frame
        points = [e_raw] if item.late_start else [s_raw, e_raw]
        for z in points:
            low, high = _bounds(seg.envelope, z)
            if low is not None:
                self._lat_low = (self._lat_low + [low])[-30:]
            if high is not None:
                self._lat_high = (self._lat_high + [high])[-30:]
        return {"item": item, "s_raw": s_raw, "e_raw": e_raw, "stalls": stalls}

    def _save(self, prepared: dict) -> None:
        item, s_raw, e_raw, stalls = prepared["item"], prepared["s_raw"], prepared["e_raw"], prepared["stalls"]
        seg, entry, env = item.seg, item.entry, item.seg.envelope
        latency = _estimate_latency(self._lat_low, self._lat_high)
        s_cut, s_moved = _refine(env, s_raw + latency)
        e_cut, e_moved = _refine(env, e_raw + latency)
        if DEBUG:
            ms = lambda f: f"{f / RATE * 1000:7.1f}"  # noqa: E731
            print(f"[reelcast] {entry.title[:24]:24} start {ms(s_raw)}→{ms(s_cut)}{'*' if s_moved else ' '} "
                  f"end {ms(e_raw)}→{ms(e_cut)}{'*' if e_moved else ' '} latency {ms(latency)} "
                  f"low {[round(v / RATE * 1000) for v in self._lat_low]} "
                  f"high {[round(v / RATE * 1000) for v in self._lat_high]}", file=sys.stderr)
        e_cut = min(e_cut, seg.frames)
        span = env[max(0, s_cut // ENV_BLOCK): max(0, e_cut // ENV_BLOCK)]
        if not len(span) or max(span) < SOUND:
            raise RuntimeError("no sound was recorded – was Spotify playing on another device?")
        keep = _without_stalls([(s_cut, e_cut)], stalls, seg.first_frame, env)
        total = sum(b - a for a, b in keep) / RATE
        if total < entry.length - 3:
            raise RuntimeError(f"recording too short ({fmt_duration(total)} of {fmt_duration(entry.length)})")

        tags, dest, cover_url, extra_cover = self._describe(item)
        if dest.exists() and not self.opts.overwrite:
            entry.state, entry.path, entry.reason = "exists", dest, "already saved"
            seg.path.unlink(missing_ok=True)
            self.rep.entry(entry)
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        cover = encode.fetch_cover(cover_url) or encode.fetch_cover(extra_cover())
        paths.decorate(self.opts.out_dir, dest.parent, cover)
        encode.finalize(seg.path, dest, tags, self.opts.fmt, self.opts.quality, cover,
                        Reporter(), CancelToken(), segments=keep)
        seg.path.unlink(missing_ok=True)
        entry.state, entry.path, entry.duration = "saved", dest, total
        self.rep.entry(entry)

    def _describe(self, item: _Item):
        meta, entry, ext = item.meta, item.entry, self.opts.fmt
        root = self.opts.out_dir
        note = f"Recorded from Spotify – Reelcast {__version__}"
        if entry.kind == "episode":
            ep_id = _item_id(meta)
            try:
                ep = fetch_episode(ep_id)
            except Exception:
                ep = None
            show = (ep.show if ep else "") or str(meta.get("xesam:album") or "") or entry.subtitle
            title = (ep.title if ep else "") or entry.title
            tags = {
                "title": title, "artist": show, "album_artist": show, "album": show,
                "date": ep.release if ep else "", "genre": "Podcast",
                "comment": f"https://open.spotify.com/episode/{ep_id} – {note}",
            }
            cover_url = (ep.cover_url if ep else "") or str(meta.get("mpris:artUrl") or "")
            return tags, paths.episode_path(root, show, title, ext), cover_url, lambda: artwork.show_artwork_url(show)

        artist = _join(meta.get("xesam:artist"))
        album_artist = _join(meta.get("xesam:albumArtist")) or artist
        album = str(meta.get("xesam:album") or "")
        number = int(meta.get("xesam:trackNumber") or 0)
        disc = int(meta.get("xesam:discNumber") or 0)
        track_id = _item_id(meta)
        year = fetch_track_release(track_id)
        tags = {
            "title": entry.title, "artist": artist, "album_artist": album_artist, "album": album,
            "track": str(number) if number else "", "disc": str(disc) if disc else "", "date": year,
            "comment": f"https://open.spotify.com/track/{track_id} – {note}",
        }
        dest = paths.track_path(root, album_artist, album, entry.title, number, disc, ext)
        cover_url = str(meta.get("mpris:artUrl") or "")
        return tags, dest, cover_url, lambda: artwork.track_artwork_url(artist, entry.title, album)


def _refine(env, est: float) -> tuple[int, bool]:
    """Decide the cut point at a track boundary.

    If the estimate lies in silence, it stays as it is: there's silence on both
    sides and the track length stays exact. If it lies in sound but close to
    silence, the estimate is off there – then the cut moves to the nearest edge
    between sound and silence. Without silence nearby (gapless transitions) the
    estimate is used.

    Returns (frame, whether it was moved to an edge).
    """
    n = len(env)
    b = int(est // ENV_BLOCK)
    if not n or b < 0 or b >= n or env[b] < QUIET:
        return max(0, int(est)), False
    w = int(SEARCH * RATE // ENV_BLOCK)
    lo, hi = max(0, b - w), min(n - 1, b + w)
    edges = [
        k for k in range(lo, hi)
        if (env[k] < QUIET) != (env[k + 1] < QUIET)
    ]
    if not edges:
        return max(0, int(est)), False
    k = min(edges, key=lambda e: abs((e + 1) * ENV_BLOCK - est))
    return (k + 1) * ENV_BLOCK, True


def _bounds(env, z: float) -> tuple[float | None, float | None]:
    """Bounds on the latency from the silence at a track boundary.

    `z` is the boundary according to Spotify's position (without latency). The last
    sound before the silence lies at most at the true boundary (→ lower bound), the
    first sound after it at least there (→ upper bound).
    """
    n = len(env)
    lo = int((z - 0.3 * RATE) // ENV_BLOCK)
    hi = int((z + 0.6 * RATE) // ENV_BLOCK)
    best = None
    i = max(0, lo)
    while i <= min(hi, n - 1):
        if env[i] < QUIET:
            j = i
            while j < n and env[j] < QUIET:
                j += 1
            k = i
            while k > 0 and env[k - 1] < QUIET:
                k -= 1
            if j - k >= 3 and (best is None or j - k > best[1] - best[0]):
                best = (k, j)
            i = j
        else:
            i += 1
    if not best:
        return None, None
    qa, qb = best
    low = qa * ENV_BLOCK - z if qa > 0 and qa >= lo else None
    high = qb * ENV_BLOCK - z if qb < n and qb <= hi else None
    return low, high


def _estimate_latency(lows: list[float], highs: list[float]) -> float:
    """Latency that satisfies most measured bounds (robust against outliers)."""
    if not lows and not highs:
        return 0.0
    tol = 0.005 * RATE
    grid = np.arange(-0.3 * RATE, 0.6 * RATE + 1, 0.005 * RATE)
    score = np.zeros(len(grid))
    for v in lows:
        score += grid >= v - tol
    for v in highs:
        score += grid <= v + tol
    best = np.flatnonzero(score == score.max())
    # take the largest contiguous range
    runs, cur = [], [best[0]]
    for i in best[1:]:
        if i == cur[-1] + 1:
            cur.append(i)
        else:
            runs.append(cur)
            cur = [i]
    runs.append(cur)
    run = max(runs, key=len)
    a, b = grid[run[0]], grid[run[-1]]
    # A range that reaches the edge of the search grid isn't bounded by a measurement
    # there – then only the measured side counts.
    open_low, open_high = run[0] == 0, run[-1] == len(grid) - 1
    if open_low and not open_high:
        return float(b)
    if open_high and not open_low:
        return float(a)
    return float((a + b) / 2)


def _without_stalls(keep, stalls, first_frame, env) -> list[tuple[int, int]]:
    """Cut out pause/buffering spots – only where there really is digital zero."""
    out = list(keep)
    for a_abs, b_abs, want in stalls:
        a, b = a_abs - first_frame, b_abs - first_frame
        want = int(want)
        if want < MIN_STALL * RATE:
            continue
        # look for digital zero around the pause
        ba, bb = max(0, a // ENV_BLOCK - 20), min(len(env), b // ENV_BLOCK + 20)
        best = None
        i = ba
        while i < bb:
            if env[i] < ZERO:
                j = i
                while j < len(env) and env[j] < ZERO:
                    j += 1
                if best is None or (j - i) > (best[1] - best[0]):
                    best = (i, j)
                i = j
            else:
                i += 1
        if not best:
            if DEBUG:
                print(f"[session] stall at {a / RATE:7.2f} s: want {want / RATE * 1000:5.0f} ms, "
                      "no digital zero – kept", file=sys.stderr)
            continue
        ra, rb = best[0] * ENV_BLOCK, best[1] * ENV_BLOCK
        cut = min(want, rb - ra)
        if DEBUG:
            print(f"[session] stall at {a / RATE:7.2f} s: want {want / RATE * 1000:5.0f} ms, "
                  f"digital zero {(rb - ra) / RATE * 1000:5.0f} ms, cut {cut / RATE * 1000:5.0f} ms", file=sys.stderr)
        ca = ra + (rb - ra - cut) // 2
        cb = ca + cut
        nxt = []
        for s, e in out:
            if cb <= s or ca >= e:
                nxt.append((s, e))
                continue
            if ca > s:
                nxt.append((s, ca))
            if cb < e:
                nxt.append((cb, e))
        out = nxt
    return [(int(s), int(e)) for s, e in out if e - s > 0]
