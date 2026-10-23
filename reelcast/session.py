"""Auto-Mitschnitt: alles, was Spotify auf diesem Rechner spielt, titelgenau mitschneiden.

Reelcast hält Spotify dauerhaft auf dem Aufnahme-Ausgang und nimmt ohne Pause auf.
Bei jedem Titelwechsel wird die Daueraufnahme geteilt:

* **Grenzen** kommen aus Spotifys Position: Frame der Aufnahme minus Position ×
  Abtastrate ergibt, wo im Strom der Titel bei 0:00 lag. Der Mittelwert vieler
  Messungen ist auf wenige Millisekunden genau. Liegt an der Grenze Stille, wird
  genau dort geschnitten. Gemessene Abweichungen eichen die Schätzung für
  lückenlose Übergänge nach.
* **Pausen und Puffern** erkennt Reelcast daran, dass Spotifys Position steht,
  während die Aufnahme weiterläuft. Nur diese Stellen werden herausgeschnitten,
  gewollte Stille in Musikstücken bleibt erhalten.
* **Gespeichert** wird nur, was vollständig lief: von 0:00 bis zum Ende, ohne
  Spulen, mit 1× Tempo. Werbung und lokale Dateien werden ignoriert.
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
from .audio import ENV_BLOCK, RATE, CaptureSink, Segment, StreamCapture
from .model import CancelToken, Cancelled, Options, Reporter, fmt_duration
from .player import Spotify
from .spotify_meta import fetch_episode, fetch_track_release

TICK = 0.1
ROUTE_EVERY = 0.5  # Sekunden zwischen Routing-/Lautstärke-Prüfungen
MARGIN = 2.0  # Rand vor und nach jedem Titel in der Rohdatei
SEARCH = 0.6  # Suchfenster für den Schnitt in Stille
START_TOL = 1.5  # so spät darf ein Titel erstmals gesehen werden und gilt als „von vorn“
END_TOL = 2.5  # so nah am Ende muss der letzte gesehene Stand liegen
SPEED_TOL = 0.04
DEBUG = bool(os.environ.get("REELCAST_DEBUG"))
QUIET = 10 ** (-85 / 20)  # Stille für den Feinschnitt (praktisch digitale Null)
ZERO = 1e-6  # digitale Null (Pause/Puffern)


@dataclass
class Entry:
    """Ein Eintrag im Logbuch der Sitzung (für die Anzeige)."""

    key: int
    kind: str  # track | episode | ad | other
    title: str
    subtitle: str
    length: float
    state: str = "recording"  # recording | partial | encoding | saved | exists | skipped | failed
    reason: str = ""
    path: Path | None = None
    duration: float = 0.0


class SessionReporter(Reporter):
    def status(self, text: str) -> None: ...

    def now_playing(self, entry: Entry | None) -> None: ...

    def position(self, pos: float, length: float) -> None: ...

    def entry(self, entry: Entry) -> None:
        """Neuer oder geänderter Logbuch-Eintrag."""


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
        # Laufzeit Spotify-Position → Aufnahme: jede Titelgrenze mit Stille liefert eine
        # Unter- bzw. Obergrenze (Frames); daraus wird die Verzögerung geschätzt.
        self._lat_low: list[float] = []
        self._lat_high: list[float] = []
        self._open_boundary: _Boundary | None = None
        self.workdir = opts.out_dir / ".reelcast-tmp"

    # ---- Ablauf -------------------------------------------------------------

    def run(self) -> None:
        prefs = doctor.spotify_prefs()
        if prefs.get("audio.crossfade_v2") == "true":
            self.rep.warn("Crossfade ist in Spotify an – Übergänge werden überblendet und Titel "
                          "lassen sich nicht sauber trennen. (Einstellungen → Wiedergabe)")
        sp = Spotify()
        worker = threading.Thread(target=self._encoder, name="encoder", daemon=True)
        worker.start()
        try:
            with CaptureSink(listen=self.opts.listen) as sink:
                sink.route()
                sink.watch()
                cap = StreamCapture(sink.name, self.workdir, on_level=self.rep.level)
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
                        self._finish(item, cap, natural=False, reason="Spotify wurde beendet")
                        item = None
                        self.rep.now_playing(None)
                    show("Warte auf Spotify …")
                    continue
                if now >= next_route:
                    next_route = now + ROUTE_EVERY
                    sink.route()
                    if sink.enforce_full_volume() and not warned_volume:
                        warned_volume = True
                        self.rep.info("Spotify bleibt für die Aufnahme auf 100 % – Lautstärke bitte am System regeln.")

                status, meta, pos = sp.snapshot()
                frame = cap.frames
                if sink.take_late_routes() and item and self._restart(sp, item, cap):
                    item = None
                    continue
                tid = str(meta.get("mpris:trackid", ""))
                if status == "Unknown":
                    continue
                if not tid or status == "Stopped" or pos is None:
                    if item:
                        self._finish(item, cap, natural=self._at_end(item, now, status))
                        item = None
                        self.rep.now_playing(None)
                    show("Bereit – spiel etwas in Spotify ab")
                    continue

                restarted = item is not None and tid == item.trackid and pos + 2.0 < item.last_pos
                if item is None or tid != item.trackid or restarted:
                    if item:
                        self._finish(item, cap, natural=self._at_end(item, now, "Playing"))
                    item = self._begin(meta, tid, pos, frame, now, cap)
                    show("Nimmt auf" if item.seg else "Ignoriert")
                    continue

                self._track(item, status, pos, frame, now)
                show("Pausiert" if status == "Paused" else ("Nimmt auf" if item.seg else "Ignoriert"))
                self.rep.position(pos, item.entry.length)
        except Cancelled:
            pass
        finally:
            if item:
                self._finish(item, cap, natural=False, reason="Aufnahme beendet")
                self.rep.now_playing(None)

    # ---- Titel verfolgen -------------------------------------------------------

    def _begin(self, meta: dict, tid: str, pos: float, frame: int, now: float, cap: StreamCapture) -> _Item:
        kind = _kind(tid)
        title = str(meta.get("xesam:title") or "Unbekannt")
        if kind == "episode":
            subtitle = str(meta.get("xesam:album") or _join(meta.get("xesam:artist")))
        else:
            subtitle = " · ".join(x for x in (_join(meta.get("xesam:artist")), str(meta.get("xesam:album") or "")) if x)
        length = (meta.get("mpris:length") or 0) / 1e6
        entry = Entry(next(self._keys), kind, title, subtitle, length)
        seg = None
        if kind == "ad":
            entry.state, entry.reason = "skipped", "Werbung – ignoriert"
        elif kind == "other":
            entry.state, entry.reason = "skipped", "lokale Datei"
        elif not length:
            entry.state, entry.reason = "skipped", "Spotify meldet keine Länge"
        else:
            if pos > START_TOL:
                entry.state, entry.reason = "partial", "nicht von Anfang an"
            seg = cap.open_segment(int(frame - (pos + MARGIN) * RATE), f"titel-{entry.key}")
        item = _Item(entry, tid, meta, seg, first_pos=pos, last_pos=pos, last_wall=now, last_frame=frame)
        item.z_runs[0].append(frame - pos * RATE)
        item.prev_boundary, self._open_boundary = self._open_boundary, None
        if item.prev_boundary and pos > START_TOL:
            item.prev_boundary.set(None)  # kein direkter Übergang
        self.entries.append(entry)
        self.rep.entry(entry)
        self.rep.now_playing(entry)
        return item

    def _track(self, item: _Item, status: str, pos: float, frame: int, now: float) -> None:
        dt = now - item.last_wall
        dpos = pos - item.last_pos
        if status == "Playing" and dt > 0:
            if dpos < -0.5 or dpos > dt + 1.0:
                item.seeked = True
                if item.entry.state == "recording":
                    item.entry.state, item.entry.reason = "partial", "gespult"
                    self.rep.entry(item.entry)
            elif dpos < 0.3 * dt:
                if item.stall_open is None:  # Puffern: Position steht, Aufnahme läuft
                    item.stall_open = item.last_frame
            else:
                if item.stall_open is not None:
                    item.stalls.append((item.stall_open, item.last_frame))
                    item.stall_open = None
                    item.z_runs.append([])
                item.played += dt
                item.advanced += dpos
                item.z_runs[-1].append(frame - pos * RATE)
                if item.prev_boundary and len(item.z_runs) == 1 and len(item.z_runs[0]) >= 10:
                    item.prev_boundary.set(statistics.median(item.z_runs[0]))
                if item.speed is None and item.played >= 3.0 and item.entry.kind == "episode":
                    ratio = item.advanced / item.played
                    if abs(ratio - 1.0) > SPEED_TOL:
                        item.speed = ratio
                        shown = f"{round(ratio * 20) / 20:.2f}".rstrip("0").rstrip(".").replace(".", ",")
                        if item.entry.state == "recording":
                            item.entry.state, item.entry.reason = "partial", f"Tempo {shown}×"
                            self.rep.entry(item.entry)
                    else:
                        item.speed = 1.0
        elif status == "Paused" and item.stall_open is None:
            item.stall_open = item.last_frame
            if item.prev_boundary:
                item.prev_boundary.set(statistics.median(item.z_runs[0]) if item.z_runs[0] else None)
        item.last_pos, item.last_wall, item.last_frame = pos, now, frame

    def _restart(self, sp: Spotify, item: _Item, cap: StreamCapture) -> bool:
        """Spotify-Strom kam erst nach Titelbeginn an → Anfang fehlt: einmal von vorn."""
        if item.seg is None or item.entry.state != "recording" or item.last_pos > 3.0:
            return False
        sp.set_position(item.trackid, 0)
        cap.end_segment(item.seg, cap.frames)
        threading.Thread(target=item.seg.discard, daemon=True).start()
        item.entry.state = "dropped"
        self.entries.remove(item.entry)
        self.rep.entry(item.entry)
        self.rep.info("Spotify-Ton kam einen Moment zu spät an – Titel wurde kurz an den Anfang gesetzt.")
        return True

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
            entry.state, entry.reason = "partial", reason or "übersprungen"
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
        # Länge jeder Pause exakt aus dem Sprung der Schätzung davor/danach
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

    # ---- Schneiden und Speichern (eigener Thread) ---------------------------

    def _encoder(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            item, start, end, stalls, boundary = job
            try:
                self._save(item, start, end, stalls, boundary)
            except Exception as e:
                item.entry.state, item.entry.reason = "failed", str(e)[:200]
                self.rep.entry(item.entry)
                if item.seg:
                    item.seg.path.unlink(missing_ok=True)

    def _save(self, item: _Item, start: float, end: float, stalls, boundary) -> None:
        seg = item.seg
        seg.done.wait()
        if boundary:
            boundary.ready.wait(3.0)
            # Lückenloser Übergang: der Anfang des Folgetitels ist das genauere Ende
            if boundary.z is not None and abs(boundary.z - end) < 0.5 * RATE:
                end = boundary.z
        entry = item.entry
        if seg.error:
            raise RuntimeError(seg.error)
        env = seg.envelope
        s_raw, e_raw = start - seg.first_frame, end - seg.first_frame
        for z in (s_raw, e_raw):
            low, high = _bounds(env, z)
            if low is not None:
                self._lat_low = (self._lat_low + [low])[-30:]
            if high is not None:
                self._lat_high = (self._lat_high + [high])[-30:]
        latency = _estimate_latency(self._lat_low, self._lat_high)
        s_cut, s_moved = _refine(env, s_raw + latency)
        e_cut, e_moved = _refine(env, e_raw + latency)
        if DEBUG:
            ms = lambda f: f"{f / RATE * 1000:7.1f}"  # noqa: E731
            print(f"[reelcast] {entry.title[:24]:24} start {ms(s_raw)}→{ms(s_cut)}{'*' if s_moved else ' '} "
                  f"ende {ms(e_raw)}→{ms(e_cut)}{'*' if e_moved else ' '} laufzeit {ms(latency)} "
                  f"unten {[round(v / RATE * 1000) for v in self._lat_low]} "
                  f"oben {[round(v / RATE * 1000) for v in self._lat_high]}", file=sys.stderr)
        e_cut = min(e_cut, seg.frames)
        keep = _without_stalls([(s_cut, e_cut)], stalls, seg.first_frame, env)
        total = sum(b - a for a, b in keep) / RATE
        if total < entry.length - 3:
            raise RuntimeError(f"Aufnahme zu kurz ({fmt_duration(total)} von {fmt_duration(entry.length)})")

        tags, dest, cover_url, extra_cover = self._describe(item)
        if dest.exists() and not self.opts.overwrite:
            entry.state, entry.path, entry.reason = "exists", dest, "schon vorhanden"
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
        note = f"Spotify-Mitschnitt – Reelcast {__version__}"
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
    """Schnittpunkt an einer Titelgrenze festlegen.

    Liegt die Schätzung in Stille, bleibt sie, wie sie ist: davor und danach ist
    Stille, die Länge des Titels bleibt exakt. Liegt sie im Ton, aber nahe an
    Stille, ist die Schätzung dort ungenau – dann wird an die nächste Kante
    zwischen Ton und Stille geschnitten. Ohne Stille in der Nähe (lückenlose
    Übergänge) gilt die Schätzung.

    Gibt (Frame, ob an eine Kante verschoben wurde) zurück.
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
    """Grenzen für die Laufzeit aus der Stille an einer Titelgrenze.

    `z` ist die Grenze laut Spotify-Position (ohne Laufzeit). Der letzte Ton vor
    der Stille liegt höchstens bei der echten Grenze (→ Untergrenze), der erste
    Ton danach mindestens dort (→ Obergrenze).
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
    """Laufzeit, die die meisten gemessenen Grenzen erfüllt (robust gegen Ausreißer)."""
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
    # größten zusammenhängenden Bereich nehmen
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
    # Ein Bereich, der bis an den Rand des Suchrasters reicht, ist dort nicht durch eine
    # Messung begrenzt – dann zählt nur die gemessene Seite.
    open_low, open_high = run[0] == 0, run[-1] == len(grid) - 1
    if open_low and not open_high:
        return float(b)
    if open_high and not open_low:
        return float(a)
    return float((a + b) / 2)


def _without_stalls(keep, stalls, first_frame, env) -> list[tuple[int, int]]:
    """Pausen/Puffer-Stellen herausschneiden – nur dort, wo wirklich digitale Null liegt."""
    out = list(keep)
    for a_abs, b_abs, want in stalls:
        a, b = a_abs - first_frame, b_abs - first_frame
        want = int(want)
        if want < RATE // 5:
            continue
        # digitale Null rund um die Pause suchen
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
            continue
        ra, rb = best[0] * ENV_BLOCK, best[1] * ENV_BLOCK
        cut = min(want, rb - ra)
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
