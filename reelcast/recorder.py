"""Aufnahme-Weg: Spotify spielt die Folge, Reelcast nimmt genau diesen Strom auf.

Ablauf
  1. Spotify starten (falls nötig, minimiert) und per MPRIS die Folge öffnen.
  2. Sofort pausieren, Spotify-Strom auf den virtuellen Ausgang legen, Lautstärke
     auf 100 % (= unverändertes Signal), an den Anfang springen.
  3. Aufnahme starten, dann Wiedergabe. Ein Wächter hält Spotify während der
     ganzen Folge auf dem Aufnahme-Ausgang und erkennt Ende, Pausen, Abbrüche.
  4. Aufräumen: Strom und Lautstärke zurück, virtuellen Ausgang entfernen.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from .audio import RATE, Capture, CaptureSink
from .model import CancelToken, Episode, ReelcastError, Options, Reporter
from .player import Spotify

TICK = 0.2
END_TOLERANCE = 3.0  # so nah am Ende gilt eine Pause als „Folge zu Ende“
NO_SOUND_TIMEOUT = 25.0
SPEED_WINDOW = 2.5  # Sekunden aufgenommenes Audio pro Tempo-Messung
SPEED_TOL = 0.04


class SpeedError(ReelcastError):
    """Spotify spielt nicht mit 1× (Podcast-Tempo)."""

    def __init__(self, speed: float):
        self.speed = speed
        shown = f"{speed:.2f}".rstrip("0").rstrip(".").replace(".", ",")
        super().__init__(
            f"Spotify spielt diese Folge mit {shown}× statt 1× – die Aufnahme wäre gestaucht. "
            f"Stell in Spotify das Podcast-Tempo auf 1× (Wiedergabeleiste, Schaltfläche „{shown}x“) "
            "und starte erneut."
        )


@dataclass
class Recording:
    flac: Path
    segments: list[tuple[int, int]]
    expected: float
    content: float


def _open_episode(sp: Spotify, ep: Episode, cancel: CancelToken, timeout: float = 40) -> dict:
    """Folge öffnen und warten, bis Spotify sie geladen hat."""
    deadline = time.monotonic() + timeout
    next_try = 0.0
    while True:
        now = time.monotonic()
        meta = sp.metadata()
        if Spotify.matches(meta, ep.id) and sp.status() in ("Playing", "Paused"):
            return meta
        if now > deadline:
            raise ReelcastError(
                "Spotify lädt die Folge nicht. Bist du in Spotify angemeldet und online?"
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
    reporter.phase("Starte Spotify …" if not sp.running() else "Verbinde mit Spotify …")
    sp.ensure_running(cancel, minimize=not opts.show_spotify)

    if sp.status() == "Playing" and not Spotify.matches(sp.metadata(), ep.id) and not opts.force:
        raise ReelcastError(
            "Spotify spielt gerade etwas anderes. Erst pausieren – oder mit --erzwingen unterbrechen."
        )

    old_volume = sp.volume()
    muted = old_volume is not None and sp.set_volume(0.0)  # kein Ton auf die Lautsprecher beim Laden
    cap: Capture | None = None
    try:
        with CaptureSink(listen=opts.listen) as sink:
            sink.route()  # ein schon bestehender (pausierter) Spotify-Strom
            reporter.phase("Öffne Folge in Spotify …")
            meta = _open_episode(sp, ep, cancel)
            sp.pause()
            _wait_status(sp, {"Paused", "Stopped"}, cancel)
            trackid = str(meta.get("mpris:trackid", ""))
            expected = (meta.get("mpris:length") or 0) / 1e6 or ep.duration
            if not expected:
                raise ReelcastError("Spotify meldet keine Länge für diese Folge.")

            sink.route()
            if muted:
                sp.set_volume(1.0)
            sp.set_position(trackid, 0)
            cancel.sleep(0.3)

            cap = Capture(sink.name, flac_path, on_level=reporter.level)
            cap.start()
            sp.play()
            if not _wait_status(sp, {"Playing"}, cancel, timeout=8):
                raise ReelcastError("Spotify startet die Wiedergabe nicht.")
            sp.release_window()
            reporter.phase("Nehme auf …")
            _watch(sp, sink, cap, ep, trackid, expected, opts, reporter, cancel)
            time.sleep(0.4)  # letzte Puffer durch die Leitung lassen
            cap.stop()
            sp.pause()
            if cap.error:
                raise ReelcastError(cap.error)
            segs = cap.segments(max_seconds=expected + 0.25)
            content = sum(b - a for a, b in segs) / RATE
            if not segs:
                raise ReelcastError("Die Aufnahme enthält keinen Ton.")
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
    # Spotify meldet sein Podcast-Tempo nicht (MPRIS-Rate bleibt 1.0). Deshalb wird es gemessen:
    # Fortschritt der Spotify-Position geteilt durch Fortschritt des aufgenommenen Audios.
    # Beides steht beim Puffern still, das Verhältnis bleibt also auch dann aussagekräftig.
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
                # Spotify hat den Strom erst beim Abspielen neu angelegt → Anfang fehlt; nochmal von vorn
                cap.mark_restart()
                sp.set_position(trackid, 0)
                reseeked = True
                speed_ref = None
                continue
            reporter.warn("Spotify hat seinen Audiostrom neu aufgebaut – wieder umgeleitet.")

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
                    raise ReelcastError("Die Position in Spotify wurde während der Aufnahme verändert.")
                else:
                    speed_strikes += 1
                    if speed_strikes >= 2:  # zweimal hintereinander = kein Messfehler
                        sp.pause()
                        raise SpeedError(round(speed * 20) / 20)
        elif status != "Playing":
            speed_ref = None

        if not Spotify.matches(meta, ep.id):
            sp.pause()  # nächste Folge/Autoplay: sofort anhalten
            if content < expected - END_TOLERANCE:
                raise ReelcastError(
                    "Spotify ist vorzeitig zu einem anderen Titel gewechselt "
                    f"(nach {content:.0f} von {expected:.0f} s)."
                )
            return
        if content >= expected + 2:
            sp.pause()
            return
        if status == "Stopped":
            if content < expected - END_TOLERANCE:
                raise ReelcastError("Spotify hat die Wiedergabe beendet, bevor die Folge zu Ende war.")
            return
        if status == "Paused":
            if content >= expected - END_TOLERANCE:
                return
            paused_since = paused_since or now
            if now - paused_since > 2 and not warned_pause:
                warned_pause = True
                reporter.warn("Spotify wurde pausiert – die Aufnahme wartet (Lücke wird später entfernt).")
            continue
        if paused_since:
            paused_since = None
            warned_pause = False

        # Spielt, aber es kommt nichts an?
        if cap.first_sound is None and now - started > NO_SOUND_TIMEOUT:
            if n_streams == 0:
                raise ReelcastError(
                    "Spotify spielt, aber nicht auf diesem Rechner – vermutlich ist per Spotify "
                    "Connect ein anderes Gerät aktiv. In Spotify „Dieser Computer“ wählen."
                )
            raise ReelcastError("Spotify spielt, aber es kommt kein Ton an.")
        if cap.first_sound is not None and cap.silent_for > 20 and not warned_silence:
            warned_silence = True
            reporter.warn("Seit 20 s nur Stille – puffert Spotify? Die Aufnahme läuft weiter.")
        elif cap.silent_for < 1:
            warned_silence = False
