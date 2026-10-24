"""Self-test with the real Spotify app – runs on Linux and macOS (also inside Reelcast.app).

Drives the real Reelcast window (Auto Record on), plays an album in Spotify and
provokes the cases Reelcast has to handle, then records a single podcast episode
through the link field. Writes a report with screenshots and waveform images.

    reelcast --selftest <report dir> [--listen]

Scenario (album: Rick Astley – Whenever You Need Somebody, 2022 remaster):
  track A  plays completely, with a 4 s pause in the middle   → saved, pause removed
  track B  is skipped after 20 s                              → not saved
  track C  plays completely                                   → saved
  track D  is stopped after 5 s                               → not saved
  episode  "tagesschau in 100 Sekunden" via the link field    → saved
The recorded audio files are deleted afterwards unless keep_audio is set.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

ALBUM = "spotify:album:4C4LvbYS0pxXLW5sGD9EK5"
EPISODE = "https://open.spotify.com/episode/6L4teFulT1MD53bBA3vFkz"
PLATFORM = "macOS" if sys.platform == "darwin" else "Linux"


def run(report: Path, listen: bool = False, keep_audio: bool = False) -> int:
    from PySide6.QtCore import QSettings, QTimer
    from PySide6.QtWidgets import QApplication

    from . import gui
    from .backend import Spotify
    from .gui import ENTRY_ROLE
    from .model import CancelToken

    report = report.resolve()
    shots = report / "screenshots"
    shots.mkdir(parents=True, exist_ok=True)
    out_dir = report / "out"
    log_lines: list[str] = []

    def log(text: str) -> None:
        line = f"{time.strftime('%H:%M:%S')}  {text}"
        log_lines.append(line)
        print(line, flush=True)

    # ---- GUI ----------------------------------------------------------------

    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyleSheet(gui.STYLE)
    win = gui.MainWindow()
    win.settings = QSettings(str(report / "settings.ini"), QSettings.IniFormat)  # leave user settings alone
    win.out_dir = out_dir
    win._show_out_dir()
    win.listen.setChecked(listen)
    win.fmt.setCurrentIndex(win.fmt.findData("mp3"))
    win.show()

    requests: list[tuple[str, object]] = []
    lock = threading.Lock()

    def ask(kind: str, payload: object = None) -> None:
        with lock:
            requests.append((kind, payload))

    def pump() -> None:
        with lock:
            todo, requests[:] = list(requests), []
        for kind, payload in todo:
            if kind == "shot":
                path = shots / f"{payload}.png"
                win.grab().save(str(path))
                log(f"screenshot {path.name}")
            elif kind == "auto":
                win.auto.setChecked(bool(payload))
            elif kind == "link":
                win.link.setText(str(payload))
                win.on_link()
            elif kind == "quit":
                app.quit()

    timer = QTimer()
    timer.timeout.connect(pump)
    timer.start(100)

    def gui_busy() -> bool:
        return win.worker is not None

    # ---- driver: controls Spotify like a person would ------------------------------

    result: dict = {"platform": PLATFORM, "listen": listen, "steps": []}

    def wait_for(cond, timeout: float, what: str, poll: float = 0.2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = cond()
            if value:
                return value
            time.sleep(poll)
        raise TimeoutError(f"timed out waiting for {what}")

    def drive() -> None:
        sp = Spotify()
        try:
            log(f"Platform {PLATFORM}, listen={listen}")
            sp.ensure_running(CancelToken(), minimize=True)
            log("Spotify is running")
            ask("auto", True)
            wait_for(lambda: win.status_code in ("ready", "waiting", "recording", "paused"), 30, "Auto Record")
            time.sleep(2)
            ask("shot", "01-auto-record-ready")
            time.sleep(1)

            def track() -> tuple[str, float, float]:
                _status, meta, pos = sp.snapshot()
                return str(meta.get("mpris:trackid", "")), (meta.get("mpris:length") or 0) / 1e6, pos or 0.0

            album = _album_track_ids()
            before = track()[0]
            sp.open_uri(ALBUM)

            def on_album() -> str | None:
                tid = track()[0]
                if not tid or tid == before:
                    return None
                return tid if not album or tid.rsplit("/", 1)[-1] in album else None

            a_id = wait_for(on_album, 30, "album playback")
            time.sleep(1)
            if sp.status() != "Playing":
                sp.play()
            a_len = track()[1]
            log(f"Track A {a_id} ({a_len:.1f} s)")
            wait_for(lambda: track()[2] >= 6, 30, "A at 0:06")
            if not any(db > -60 for db in list(win.cassette.levels)):
                raise RuntimeError("no sound arrives – Spotify plays on another device (Spotify Connect)?")
            result["steps"].append({"track": "A", "id": a_id, "length": a_len, "expect": "saved"})
            wait_for(lambda: track()[2] >= 30, 60, "A at 0:30")
            ask("shot", "02-recording-track-a")
            wait_for(lambda: track()[2] >= 60, 60, "A at 1:00")
            sp.pause()
            log("Paused A for 4 s")
            time.sleep(2)
            ask("shot", "03-paused")
            time.sleep(2)
            sp.play()
            b_id = wait_for(lambda: (t := track()[0]) != a_id and t, a_len + 30, "track B")
            b_len = track()[1]
            log(f"Track B {b_id} ({b_len:.1f} s) – will be skipped after 20 s")
            result["steps"].append({"track": "B", "id": b_id, "length": b_len, "expect": "skipped"})
            wait_for(lambda: track()[2] >= 20, 40, "B at 0:20")
            sp.next_track()
            c_id = wait_for(lambda: (t := track()[0]) != b_id and t, 30, "track C")
            time.sleep(1)
            c_len = track()[1]
            log(f"Track C {c_id} ({c_len:.1f} s)")
            result["steps"].append({"track": "C", "id": c_id, "length": c_len, "expect": "saved"})
            wait_for(lambda: track()[2] >= c_len / 2, c_len, "C halfway")
            ask("shot", "04-recording-track-c")
            d_id = wait_for(lambda: (t := track()[0]) != c_id and t, c_len, "track D")
            log(f"Track D {d_id} – will be stopped after 5 s")
            result["steps"].append({"track": "D", "id": d_id, "length": track()[1], "expect": "not saved"})
            time.sleep(5)
            sp.pause()
            time.sleep(8)  # let C finish saving
            ask("shot", "05-after-album")
            ask("auto", False)
            wait_for(lambda: not gui_busy(), 60, "Auto Record to stop")
            time.sleep(1)

            log("Single episode via link field")
            ask("link", EPISODE)
            wait_for(gui_busy, 20, "episode job to start")
            time.sleep(25)
            ask("shot", "06-recording-episode")
            wait_for(lambda: not gui_busy(), 400, "episode to finish", poll=1)
            time.sleep(1)
            ask("shot", "07-done")
            time.sleep(1)
        except Exception as e:  # noqa: BLE001
            log(f"DRIVER ERROR: {type(e).__name__}: {e}")
            result["driver_error"] = f"{type(e).__name__}: {e}"
            ask("auto", False)
            time.sleep(5)
        finally:
            ask("quit")

    threading.Thread(target=drive, name="selftest-driver", daemon=True).start()
    app.exec()

    # ---- evaluation --------------------------------------------------------------

    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        log(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")

    entries = [win.log.item(i).data(ENTRY_ROLE) for i in range(win.log.count())]
    result["entries"] = [
        {"title": e.title, "kind": e.kind, "state": e.state, "reason": e.reason,
         "path": str(e.path) if e.path else "", "length": e.length, "duration": e.duration}
        for e in reversed(entries)
    ]
    for step in result["steps"]:
        match = [e for e in entries if abs(e.length - step["length"]) < 0.5 and e.kind == "track"]
        e = match[0] if match else None
        if step["expect"] == "saved":
            ok = e is not None and e.state == "saved" and e.path and Path(e.path).exists()
            check(f"Track {step['track']} saved", ok, f"state={e.state if e else 'missing'}")
            if ok:
                info = _probe(Path(e.path))
                diff = info["duration"] - step["length"]
                check(f"Track {step['track']} length", abs(diff) <= 0.15,
                      f"{info['duration']:.3f} s vs Spotify {step['length']:.3f} s ({diff * 1000:+.0f} ms)")
                check(f"Track {step['track']} tags", bool(info["tags"].get("title") and info["tags"].get("artist")),
                      f"{info['tags'].get('artist', '')} – {info['tags'].get('title', '')} "
                      f"[{info['tags'].get('album', '')}]")
                check(f"Track {step['track']} cover", info["cover"], "embedded" if info["cover"] else "none")
                wf = _waveform(Path(e.path), report / f"waveform-{step['track']}.png")
                step.update(info=info, waveform=wf)
                check(f"Track {step['track']} has sound", wf.get("peak_db", -200) > -30,
                      f"peak {wf.get('peak_db')} dBFS")
                # Songs may start or fade out quietly – only gross offsets count as errors.
                check(f"Track {step['track']} edges", wf.get("lead_s", 9) < 0.5 and wf.get("tail_s", 9) < 3.0,
                      f"quiet (< -50 dBFS) at start {wf.get('lead_s')} s, at end {wf.get('tail_s')} s")
        else:
            ok = e is None or e.state not in ("saved", "exists")
            check(f"Track {step['track']} not saved", ok,
                  f"state={e.state if e else 'not listed'} {e.reason if e else ''}".strip())

    ep = [e for e in entries if e.kind == "episode"]
    ok = bool(ep) and ep[0].state == "saved" and bool(ep[0].path) and Path(ep[0].path).exists()
    check("Episode saved", ok, f"state={ep[0].state if ep else 'missing'} {ep[0].reason if ep else ''}".strip())
    if ok:
        info = _probe(Path(ep[0].path))
        diff = info["duration"] - ep[0].length
        check("Episode length", abs(diff) <= 1.0, f"{info['duration']:.2f} s vs Spotify {ep[0].length:.2f} s")
        check("Episode tags", bool(info["tags"].get("title")),
              f"{info['tags'].get('artist', '')} – {info['tags'].get('title', '')}")
        wf = _waveform(Path(ep[0].path), report / "waveform-episode.png")
        check("Episode has sound", wf.get("peak_db", -200) > -30, f"peak {wf.get('peak_db')} dBFS")

    result["checks"] = checks
    result["passed"] = all(c["ok"] for c in checks) and bool(checks) and "driver_error" not in result
    if not keep_audio and out_dir.exists():
        shutil.rmtree(out_dir)  # the test only needs the evidence, not the music
        log("recorded audio deleted (evidence: screenshots, waveforms, result.json)")
    (report / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    (report / "log.txt").write_text("\n".join(log_lines) + "\n")
    log("RESULT: " + ("PASSED" if result["passed"] else "FAILED"))
    return 0 if result["passed"] else 1


def _album_track_ids() -> set[str]:
    """Track IDs of the test album (empty if Spotify's embed page can't be read)."""
    import re

    from . import net

    try:
        page = net.get_text("https://open.spotify.com/embed/album/" + ALBUM.rsplit(":", 1)[-1])
        data = json.loads(re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                                    page, re.S).group(1))
        tracks = data["props"]["pageProps"]["state"]["data"]["entity"]["trackList"]
        return {t["uri"].rsplit(":", 1)[-1] for t in tracks}
    except Exception:
        return set()


def _probe(path: Path) -> dict:
    from . import tools

    p = subprocess.run([tools.ffprobe(), "-v", "error", "-show_entries",
                        "format=duration:format_tags=title,artist,album,track,date,comment:stream=codec_type",
                        "-of", "json", str(path)], capture_output=True, text=True)
    d = json.loads(p.stdout or "{}")
    return {
        "duration": float(d.get("format", {}).get("duration", 0)),
        "tags": d.get("format", {}).get("tags", {}),
        "cover": any(s.get("codec_type") == "video" for s in d.get("streams", [])),
    }


def _waveform(path: Path, png: Path) -> dict:
    from PySide6.QtGui import QColor, QImage, QPainter, QPen

    from . import tools

    raw = subprocess.run([tools.ffmpeg(), "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "1",
                          "-ar", "8000", "-"], capture_output=True).stdout
    x = np.frombuffer(raw, dtype="<f4")
    w, h = 1200, 260
    img = QImage(w, h, QImage.Format_ARGB32)
    img.fill(QColor("#121016"))
    p = QPainter(img)
    p.setPen(QPen(QColor("#ffb000")))
    for i, c in enumerate(np.array_split(np.abs(x), w) if len(x) else []):
        v = float(c.max()) if len(c) else 0.0
        hh = int(v * (h / 2 - 10))
        p.drawLine(i, h // 2 - hh, i, h // 2 + hh)
    p.setPen(QColor("#f4efe6"))
    p.drawText(10, 20, f"{path.name}  ·  {len(x) / 8000:.2f} s")
    p.end()
    img.save(str(png))
    if not len(x):
        return {}
    loud = np.flatnonzero(np.abs(x) > 10 ** (-50 / 20))
    return {
        "peak_db": round(20 * np.log10(max(1e-9, float(np.abs(x).max()))), 1),
        "lead_s": round(loud[0] / 8000, 3) if len(loud) else None,
        "tail_s": round((len(x) - 1 - loud[-1]) / 8000, 3) if len(loud) else None,
    }
