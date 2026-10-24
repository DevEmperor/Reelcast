"""Render the README images: app screenshots, terminal view and banner.

Everything shown is made up – band, titles and cover art are painted here, nothing comes
from Spotify. Runs without a display:

    python3 tools/make_readme_images.py        # writes docs/images/
"""

from __future__ import annotations

import math
import os
import random
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_SCALE_FACTOR", "2")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT = ROOT / "docs" / "images"

from PySide6.QtCore import QPointF, QRectF, QSettings, Qt  # noqa: E402
from PySide6.QtGui import (  # noqa: E402
    QColor, QFont, QImage, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap, QRadialGradient,
)
from PySide6.QtSvg import QSvgRenderer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from reelcast import session as sess  # noqa: E402
from reelcast.model import PHASE_RECORDING  # noqa: E402
from reelcast.paths import ICON_SRC  # noqa: E402
from reelcast.session import Entry  # noqa: E402

BG, AMBER, CREAM, DIM = "#121016", "#ffb000", "#f4efe6", "#8a8290"
BAND, ALBUM = "Neon Harbor", "Night Drive"


# ---- cover art -------------------------------------------------------------------


def cover_album(size: int = 600) -> QPixmap:
    """Synthwave sunset: gradient sky, striped sun, perspective grid."""
    img = QImage(size, size, QImage.Format_ARGB32)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    sky = QLinearGradient(0, 0, 0, size)
    sky.setColorAt(0, QColor("#1b0b3a"))
    sky.setColorAt(0.55, QColor("#7a1f6b"))
    sky.setColorAt(1, QColor("#ff7a3d"))
    p.fillRect(0, 0, size, size, sky)
    sun = QRectF(size * 0.22, size * 0.2, size * 0.56, size * 0.56)
    grad = QLinearGradient(0, sun.top(), 0, sun.bottom())
    grad.setColorAt(0, QColor("#ffe36e"))
    grad.setColorAt(1, QColor("#ff4f8b"))
    p.setPen(Qt.NoPen)
    p.setBrush(grad)
    p.drawEllipse(sun)
    p.setBrush(QColor("#2a0f45"))
    for i in range(6):  # stripes through the lower half of the sun
        y = sun.center().y() + i * size * 0.045
        p.drawRect(QRectF(sun.left(), y, sun.width(), size * (0.006 + i * 0.004)))
    horizon = size * 0.66
    p.fillRect(QRectF(0, horizon, size, size - horizon), QColor("#140726"))
    p.setPen(QPen(QColor(255, 79, 139, 170), size / 300))
    for i in range(-10, 11):
        p.drawLine(QPointF(size / 2 + i * size * 0.03, horizon), QPointF(size / 2 + i * size * 0.16, size))
    for k in range(1, 9):
        y = horizon + (size - horizon) * (k / 8) ** 1.8
        p.drawLine(QPointF(0, y), QPointF(size, y))
    p.end()
    return QPixmap.fromImage(img)


def cover_podcast(size: int = 600) -> QPixmap:
    """Teal disc with concentric sound waves around a dot."""
    img = QImage(size, size, QImage.Format_ARGB32)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    bg = QRadialGradient(size * 0.3, size * 0.25, size)
    bg.setColorAt(0, QColor("#2ec4b6"))
    bg.setColorAt(1, QColor("#0b3c49"))
    p.fillRect(0, 0, size, size, bg)
    c = QPointF(size * 0.5, size * 0.5)
    for i, a in enumerate((230, 170, 115, 70)):
        p.setPen(QPen(QColor(244, 239, 230, a), size * 0.028))
        p.setBrush(Qt.NoBrush)
        r = size * (0.12 + i * 0.09)
        p.drawArc(QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r), 35 * 16, 110 * 16)
        p.drawArc(QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r), 215 * 16, 110 * 16)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(AMBER))
    p.drawEllipse(c, size * 0.07, size * 0.07)
    p.end()
    return QPixmap.fromImage(img)


# ---- app screenshots ---------------------------------------------------------------


def window():
    from reelcast import gui

    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyleSheet(gui.STYLE)
    win = gui.MainWindow()
    win.resize(720, 860)
    win.show()
    return app, win, gui


def settle(app: QApplication, seconds: float = 0.6) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.02)
    if app.focusWidget():  # no focus ring in the picture
        app.focusWidget().clearFocus()
        app.processEvents()


def levels(win, n: int, seed: int) -> None:
    rng = random.Random(seed)
    for i in range(n):
        win.cassette.add_level(-14 + 9 * abs(math.sin(i / 5.0)) + rng.uniform(-3, 3))


def shot_auto(path: Path) -> None:
    app, win, _ = window()
    win._busy("auto")
    win.auto.blockSignals(True)
    win.auto.setChecked(True)
    win.auto.blockSignals(False)
    sub = f"{BAND} · {ALBUM}"
    log = [
        Entry(1, "track", "Midnight Runway", sub, 214.0, state="saved", duration=213.9, artist=BAND, album=ALBUM),
        Entry(2, "ad", "Advertisement", "", 30.0, state="skipped", reason="ad – ignored"),
        Entry(3, "track", "Coastline", sub, 201.0, state="skipped", reason="skipped", artist=BAND, album=ALBUM),
        Entry(4, "track", "Glass City", sub, 188.4, state="saved", duration=188.3, artist=BAND, album=ALBUM),
    ]
    current = Entry(5, "track", "Afterglow", sub, 242.0, artist=BAND, album=ALBUM)
    for e in log + [current]:
        win.on_entry(e)
    win._covers[f"{BAND}|{ALBUM}|Afterglow"] = cover_album()
    win.on_now_playing(current)
    win.on_status(sess.RECORDING)
    win.progress.set(97, 242)
    win.on_position(97, 242)
    levels(win, 150, 1)
    settle(app)
    win.grab().save(str(path))
    win.close()


def shot_episode(path: Path) -> None:
    app, win, _ = window()
    win._busy("link")
    show = "The Signal Hour"
    past = [
        Entry(11, "episode", "Ep. 41 – Why Vinyl Came Back", show, 2311.0, state="saved", duration=2311.2),
        Entry(12, "track", "Glass City", f"{BAND} · {ALBUM}", 188.4, state="saved", duration=188.3),
    ]
    current = Entry(13, "episode", "Ep. 42 – How Tape Changed Music", show, 2735.0, album=show, artist=show)
    for e in past + [current]:
        win.on_entry(e)
    win._covers[f"{show}|{show}|{current.title}"] = cover_podcast()
    win.on_now_playing(current)
    win.on_status(sess.RECORDING)
    win.on_phase(PHASE_RECORDING)
    win.cassette.recording = True
    win.progress.set(1203, 2735)
    win.on_position(1203, 2735)
    levels(win, 150, 2)
    settle(app)
    win.grab().save(str(path))
    win.close()


# ---- terminal ----------------------------------------------------------------------------


def terminal_svg(path: Path) -> None:
    from rich.console import Console

    from reelcast import cli

    rep = cli.AutoLiveReporter()
    rep.started = time.monotonic() - 13 * 60 - 42
    sub = f"{BAND} · {ALBUM}"
    for e in [
        Entry(1, "track", "Midnight Runway", sub, 214.0, state="saved", duration=213.9),
        Entry(2, "ad", "Advertisement", "", 30.0, state="skipped", reason="ad – ignored"),
        Entry(3, "track", "Coastline", sub, 201.0, state="skipped", reason="skipped"),
        Entry(4, "track", "Glass City", sub, 188.4, state="saved", duration=188.3),
    ]:
        rep.entry(e)
    current = Entry(5, "track", "Afterglow", sub, 242.0)
    rep.entry(current)
    rep.now_playing(current)
    rep.status(sess.RECORDING)
    rep.position(97, 242)
    rng = random.Random(3)
    for i in range(64 * 3):
        rep.level(-14 + 9 * abs(math.sin(i / 6.0)) + rng.uniform(-3, 3))
    con = Console(record=True, width=78, force_terminal=True, color_system="truecolor")
    cli.console = con
    con.print(rep.render())
    con.save_svg(str(path), title="reelcast --auto")


# ---- banner --------------------------------------------------------------------------------


def banner(path: Path, shot: Path) -> None:
    w, h = 1280, 640
    img = QImage(w, h, QImage.Format_ARGB32)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.SmoothPixmapTransform)
    p.fillRect(0, 0, w, h, QColor(BG))
    glow = QRadialGradient(w * 0.78, h * 0.5, h * 0.75)
    glow.setColorAt(0, QColor(255, 176, 0, 60))
    glow.setColorAt(1, QColor(255, 176, 0, 0))
    p.fillRect(0, 0, w, h, glow)

    QSvgRenderer(str(ICON_SRC)).render(p, QRectF(80, 150, 120, 120))
    f = QFont()
    f.setPixelSize(72)
    f.setWeight(QFont.Black)
    f.setLetterSpacing(QFont.AbsoluteSpacing, 10)
    p.setFont(f)
    p.setPen(QColor(AMBER))
    p.drawText(QRectF(80, 285, 700, 90), Qt.AlignLeft | Qt.AlignVCenter, "REELCAST")
    f.setPixelSize(30)
    f.setWeight(QFont.DemiBold)
    f.setLetterSpacing(QFont.AbsoluteSpacing, 0)
    p.setFont(f)
    p.setPen(QColor(CREAM))
    p.drawText(QRectF(84, 375, 700, 44), Qt.AlignLeft | Qt.AlignVCenter, "Record Spotify, track by track.")
    f.setPixelSize(22)
    f.setWeight(QFont.Normal)
    p.setFont(f)
    p.setPen(QColor(DIM))
    p.drawText(QRectF(84, 420, 700, 36), Qt.AlignLeft | Qt.AlignVCenter, "Linux  ·  macOS  ·  open source")

    app_img = QImage(str(shot))
    target_h = h - 70
    scaled = app_img.scaledToHeight(target_h * 2, Qt.SmoothTransformation)
    sw = scaled.width() / 2
    rect = QRectF(w - sw - 70, 35, sw, target_h)
    for i in range(14, 0, -1):  # soft shadow
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 10))
        p.drawRoundedRect(rect.adjusted(-i, -i + 8, i, i + 8), 18 + i, 18 + i)
    clip = QPainterPath()
    clip.addRoundedRect(rect, 18, 18)
    p.setClipPath(clip)
    p.drawImage(rect, scaled)
    p.end()
    img.save(str(path))


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    # Keep the user's own Reelcast settings out of it (output folder, format, …).
    QSettings.setPath(QSettings.NativeFormat, QSettings.UserScope, tempfile.mkdtemp())
    QSettings.setDefaultFormat(QSettings.NativeFormat)
    shot_auto(OUT / "app-auto-record.png")
    shot_episode(OUT / "app-episode.png")
    terminal_svg(OUT / "terminal-auto.svg")
    banner(OUT / "banner.png", OUT / "app-auto-record.png")
    for f in sorted(OUT.iterdir()):
        print(f.relative_to(ROOT), f.stat().st_size // 1024, "KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
