"""Graphical interface (Qt / PySide6) – same look on Linux and macOS."""

from __future__ import annotations

import math
import sys
import threading
import time
from collections import deque
from pathlib import Path

from PySide6.QtCore import QObject, QPointF, QRectF, QSettings, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QColor,
    QDesktopServices,
    QFont,
    QFontMetrics,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from . import APP_NAME, __version__, artwork, encode, session as sess
from .engine import Engine
from .model import PHASE_RECORDING, CancelToken, Cancelled, Episode, Options, Reporter, ReelcastError, fmt_duration
from .paths import ICON_SRC, default_out_dir
from .session import AutoSession, Entry, SessionReporter

BG = "#121016"
SURFACE = "#1c1922"
SURFACE2 = "#26222d"
LINE = "#34303c"
CREAM = "#f4efe6"
DIM = "#a09aaa"
AMBER = "#ffb000"
ORANGE = "#ff7a1a"
REC = "#ff3b30"
INK = "#1b1408"

STYLE = f"""
QWidget {{ background: {BG}; color: {CREAM}; font-size: 13px; }}
QLabel#dim {{ color: {DIM}; }}
QLabel#status {{ color: {DIM}; font-size: 13px; }}
QLineEdit {{
    background: {SURFACE}; border: 1px solid {LINE}; border-radius: 10px;
    padding: 9px 12px; selection-background-color: {AMBER}; selection-color: {INK};
}}
QLineEdit:focus {{ border-color: {AMBER}; }}
QPushButton {{
    background: {SURFACE2}; border: 1px solid {LINE}; border-radius: 10px; padding: 8px 14px;
}}
QPushButton:hover, QPushButton:focus {{ border-color: {AMBER}; outline: none; }}
QPushButton:disabled {{ color: {DIM}; border-color: {SURFACE2}; }}
QPushButton#primary {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #ffc640, stop:1 {ORANGE});
    color: {INK}; font-weight: 600; border: none;
}}
QPushButton#primary:disabled {{ background: {SURFACE2}; color: {DIM}; }}
QComboBox {{
    background: {SURFACE2}; border: 1px solid {LINE}; border-radius: 10px; padding: 7px 12px;
}}
QComboBox:hover, QComboBox:focus {{ border-color: {AMBER}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{ background: {SURFACE}; selection-background-color: {AMBER};
    selection-color: {INK}; border: 1px solid {LINE}; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 5px; border: 1px solid {LINE};
    background: {SURFACE2}; }}
QCheckBox::indicator:checked {{ background: {AMBER}; border-color: {AMBER}; }}
QCheckBox:focus {{ outline: none; }}
QListWidget {{ background: {SURFACE}; border: 1px solid {LINE}; border-radius: 14px; padding: 6px;
    outline: 0; }}
QListWidget::item {{ border: none; border-radius: 8px; }}
QListWidget::item:selected {{ background: {SURFACE2}; }}
QScrollBar:vertical {{ background: transparent; width: 8px; margin: 4px; }}
QScrollBar::handle:vertical {{ background: {LINE}; border-radius: 4px; min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QToolTip {{ background: {SURFACE2}; color: {CREAM}; border: 1px solid {LINE}; }}
"""


STATUS_TEXT = {
    "idle": "Ready",
    "error": "Something went wrong",
    sess.WAITING: "Waiting for Spotify…",
    sess.READY: "Ready – play something in Spotify",
    sess.RECORDING: "Recording",
    sess.PAUSED: "Paused",
    sess.IGNORED: "Not recording this one",
}


# ---------------------------------------------------------------------------
# Bridge from the work thread to the UI
# ---------------------------------------------------------------------------


class Bridge(QObject):
    status = Signal(str)  # session state code (session.WAITING, …)
    phase = Signal(str)  # free text for single-episode phases
    now_playing = Signal(object)
    position = Signal(float, float)
    entry = Signal(object)
    level = Signal(float)
    message = Signal(str, bool)  # text, warning?
    finished = Signal(object)  # None or error message


class QtSessionReporter(SessionReporter):
    def __init__(self, bridge: Bridge):
        self.b = bridge

    def status(self, text):
        self.b.status.emit(text)

    def now_playing(self, entry):
        self.b.now_playing.emit(entry)

    def position(self, pos, length):
        self.b.position.emit(pos, length)

    def entry(self, entry):
        self.b.entry.emit(entry)

    def level(self, peak_db):
        self.b.level.emit(peak_db)

    def info(self, text):
        self.b.message.emit(text, False)

    def warn(self, text):
        self.b.message.emit(text, True)


class QtEpisodeReporter(Reporter):
    """Map a single episode (link) onto the same display as Auto Record."""

    def __init__(self, bridge: Bridge, entry: Entry):
        self.b = bridge
        self.e = entry

    def episode(self, ep: Episode):
        self.e.title, self.e.subtitle, self.e.length = ep.title, ep.show, ep.duration
        self.e.art_url, self.e.album = ep.cover_url, ep.show
        self.b.now_playing.emit(self.e)
        self.b.entry.emit(self.e)

    def phase(self, text):
        if text == PHASE_RECORDING:
            self.b.status.emit(sess.RECORDING)
        else:
            self.b.phase.emit(text)

    def progress(self, done, total, unit="s"):
        if unit == "s" and total:
            self.b.position.emit(done, total)

    def level(self, peak_db):
        self.b.level.emit(peak_db)

    def info(self, text):
        self.b.message.emit(text, False)

    def warn(self, text):
        self.b.message.emit(text, True)


# ---------------------------------------------------------------------------
# Cassette
# ---------------------------------------------------------------------------


class Cassette(QWidget):
    """Painted cassette: cover and title on the label, reels, waveform, REC lamp."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.title = "Ready"
        self.subtitle = "Turn on Auto Record or paste a link"
        self.elided = False  # for tests: was title or subtitle shortened?
        self.cover: QPixmap | None = None
        self.progress = 0.0
        self.recording = False
        self.paused = False
        self.levels: deque = deque(maxlen=48)
        self._acc: list[float] = []
        self.angle = 0.0
        self._last = time.monotonic()
        self.setMinimumSize(420, 250)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(33)

    def sizeHint(self) -> QSize:
        return QSize(620, 360)

    def heightForWidth(self, w: int) -> int:
        return int(w / 1.68)

    def hasHeightForWidth(self) -> bool:
        return True

    def add_level(self, db: float) -> None:
        self._acc.append(db)
        if len(self._acc) >= 3:
            self.levels.append(max(self._acc))
            self._acc = []

    def _tick(self) -> None:
        now = time.monotonic()
        dt, self._last = now - self._last, now
        if self.recording and not self.paused:
            self.angle = (self.angle + dt * 160) % 360
        self.update()

    # ---- Zeichnen ---------------------------------------------------------

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        w, h = self.width(), self.height()
        bw = min(w - 4, (h - 4) * 1.68)
        bh = bw / 1.68
        x0, y0 = (w - bw) / 2, (h - bh) / 2
        u = bw / 100.0  # base unit

        # Shell
        body = QRectF(x0, y0, bw, bh)
        g = QLinearGradient(body.topLeft(), body.bottomLeft())
        g.setColorAt(0, QColor("#2e2a36"))
        g.setColorAt(1, QColor("#16141b"))
        p.setPen(Qt.NoPen)
        p.setBrush(g)
        p.drawRoundedRect(body, 6 * u, 6 * u)
        p.setBrush(QColor("#45404f"))
        for cx, cy in ((x0 + 4 * u, y0 + 4 * u), (x0 + bw - 4 * u, y0 + 4 * u),
                       (x0 + 4 * u, y0 + bh - 4 * u), (x0 + bw - 4 * u, y0 + bh - 4 * u)):
            p.drawEllipse(QPointF(cx, cy), 1.1 * u, 1.1 * u)

        # Label
        lab = QRectF(x0 + 7 * u, y0 + 6 * u, bw - 14 * u, bh * 0.66)
        lg = QLinearGradient(lab.topLeft(), lab.bottomRight())
        lg.setColorAt(0, QColor("#ffc640"))
        lg.setColorAt(1, QColor(ORANGE))
        p.setBrush(lg)
        p.drawRoundedRect(lab, 3.5 * u, 3.5 * u)
        p.setBrush(QColor(255, 255, 255, 40))
        p.drawRoundedRect(QRectF(lab.x(), lab.y(), lab.width(), 4 * u), 2 * u, 2 * u)

        # Cover + text
        band_h = lab.height() * 0.40
        cov = QRectF(lab.x() + 3 * u, lab.y() + 3 * u, band_h - 1 * u, band_h - 1 * u)
        path = QPainterPath()
        path.addRoundedRect(cov, 2 * u, 2 * u)
        p.save()
        p.setClipPath(path)
        if self.cover and not self.cover.isNull():
            pm = self.cover.scaled(cov.size().toSize() * 2, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            p.drawPixmap(cov, pm, QRectF(0, 0, pm.width(), pm.height()))
        else:
            p.fillRect(cov, QColor(INK).lighter(140))
            p.setPen(QColor(AMBER))
            f = QFont(self.font())
            f.setPixelSize(int(cov.height() * 0.5))
            p.setFont(f)
            p.drawText(cov, Qt.AlignCenter, "♪")
        p.restore()

        tx = cov.right() + 3 * u
        tw = lab.right() - tx - 9 * u
        f = QFont(self.font())
        f.setPixelSize(max(11, int(4.4 * u)))
        f.setWeight(QFont.Bold)
        p.setFont(f)
        p.setPen(QColor(INK))
        fm = QFontMetrics(f)
        shown_title = fm.elidedText(self.title, Qt.ElideRight, int(tw))
        p.drawText(QRectF(tx, cov.y() + 0.5 * u, tw, fm.height()), Qt.AlignLeft | Qt.AlignVCenter, shown_title)
        f2 = QFont(self.font())
        f2.setPixelSize(max(11, int(3.0 * u)))
        p.setFont(f2)
        p.setPen(QColor(27, 20, 8, 190))
        fm2 = QFontMetrics(f2)
        shown_sub = fm2.elidedText(self.subtitle, Qt.ElideRight, int(tw))
        p.drawText(QRectF(tx, cov.y() + 0.5 * u + fm.height() + 0.8 * u, tw, fm2.height()),
                   Qt.AlignLeft | Qt.AlignVCenter, shown_sub)
        self.elided = shown_title != self.title or shown_sub != self.subtitle

        # REC lamp
        p.setPen(Qt.NoPen)
        lamp = QPointF(lab.right() - 5 * u, lab.y() + 6 * u)
        on = self.recording and not self.paused and int(time.monotonic() * 2) % 2 == 0
        if on:
            glow = QRadialGradient(lamp, 3.2 * u)
            glow.setColorAt(0, QColor(255, 59, 48, 150))
            glow.setColorAt(1, QColor(255, 59, 48, 0))
            p.setBrush(glow)
            p.drawEllipse(lamp, 3.2 * u, 3.2 * u)
        p.setBrush(QColor(REC) if on else QColor(150, 45, 30) if self.recording else QColor(140, 80, 20))
        p.drawEllipse(lamp, 1.3 * u, 1.3 * u)

        # Window
        win_h = lab.height() * 0.42
        win = QRectF(lab.x() + 10 * u, lab.bottom() - win_h - 3.5 * u, lab.width() - 20 * u, win_h)
        p.setBrush(QColor("#16141b"))
        p.drawRoundedRect(win, win_h / 2, win_h / 2)
        clip = QPainterPath()
        clip.addRoundedRect(win, win_h / 2, win_h / 2)
        p.save()
        p.setClipPath(clip)
        hub_r = win_h * 0.30
        max_r = win_h * 0.95
        left_c = QPointF(win.left() + win_h / 2, win.center().y())
        right_c = QPointF(win.right() - win_h / 2, win.center().y())
        prog = max(0.0, min(1.0, self.progress))
        for c, amount in ((left_c, 1 - prog), (right_c, prog)):
            r = hub_r * 1.15 + (max_r - hub_r * 1.15) * math.sqrt(max(0.0, amount))
            tape = QRadialGradient(c, r)
            tape.setColorAt(0, QColor("#4a3324"))
            tape.setColorAt(1, QColor("#2a1d14"))
            p.setBrush(tape)
            p.drawEllipse(c, r, r)

        # Waveform between the reels
        gap_l = left_c.x() + hub_r * 1.4
        gap_r = right_c.x() - hub_r * 1.4
        bars = list(self.levels)
        n = max(1, int((gap_r - gap_l) / (1.6 * u)))
        bars = bars[-n:]
        bar_w = (gap_r - gap_l) / n
        cy = win.center().y()
        for i in range(n):
            j = i - (n - len(bars))
            frac = 0.0
            if j >= 0:
                frac = max(0.0, min(1.0, (bars[j] + 60) / 60))
            hh = max(0.6 * u, frac * win_h * 0.42)
            col = QColor(AMBER) if frac > 0 else QColor("#3a3542")
            p.setBrush(col)
            p.drawRoundedRect(QRectF(gap_l + i * bar_w + bar_w * 0.2, cy - hh, bar_w * 0.6, 2 * hh),
                              bar_w * 0.3, bar_w * 0.3)
        p.restore()

        # Hubs with spokes
        for c, direction in ((left_c, 1), (right_c, 1)):
            p.setBrush(QColor(CREAM))
            p.drawEllipse(c, hub_r, hub_r)
            p.setBrush(QColor("#16141b"))
            p.drawEllipse(c, hub_r * 0.32, hub_r * 0.32)
            p.save()
            p.translate(c)
            p.rotate(self.angle * direction)
            for k in range(3):
                p.save()
                p.rotate(k * 120)
                p.drawRoundedRect(QRectF(-hub_r * 0.13, -hub_r * 0.92, hub_r * 0.26, hub_r * 0.42),
                                  hub_r * 0.08, hub_r * 0.08)
                p.restore()
            p.restore()

        # Tape guide at the bottom
        foot = QPainterPath()
        fy = y0 + bh
        foot.moveTo(x0 + 20 * u, fy)
        foot.lineTo(x0 + 26 * u, lab.bottom() + 4 * u)
        foot.lineTo(x0 + bw - 26 * u, lab.bottom() + 4 * u)
        foot.lineTo(x0 + bw - 20 * u, fy)
        foot.closeSubpath()
        p.setBrush(QColor("#0e0d12"))
        p.drawPath(foot)
        p.setBrush(QColor("#2e2a36"))
        for fx in (x0 + 36 * u, x0 + bw - 36 * u):
            p.drawEllipse(QPointF(fx, (lab.bottom() + 4 * u + fy) / 2), 1.6 * u, 1.6 * u)
        p.end()


# ---------------------------------------------------------------------------
# Switch, progress, log
# ---------------------------------------------------------------------------


class Toggle(QCheckBox):
    """Slide switch in Reelcast style."""

    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(28)

    def sizeHint(self) -> QSize:
        fm = self.fontMetrics()
        return QSize(52 + 10 + fm.horizontalAdvance(self.text()), 28)

    def hitButton(self, pos) -> bool:
        return self.rect().contains(pos)

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        track = QRectF(1, (self.height() - 24) / 2, 46, 24)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(AMBER) if self.isChecked() else QColor(SURFACE2))
        p.drawRoundedRect(track, 12, 12)
        knob_x = track.right() - 22 if self.isChecked() else track.left() + 2
        p.setBrush(QColor(INK) if self.isChecked() else QColor(DIM))
        p.drawEllipse(QRectF(knob_x, track.top() + 2, 20, 20))
        p.setPen(QColor(CREAM if self.isEnabled() else DIM))
        f = QFont(self.font())
        f.setWeight(QFont.DemiBold)
        p.setFont(f)
        p.drawText(QRectF(track.right() + 10, 0, self.width() - track.right() - 10, self.height()),
                   Qt.AlignVCenter | Qt.AlignLeft, self.text())


class Progress(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.pos = 0.0
        self.length = 0.0
        self.setFixedHeight(26)

    def set(self, pos: float, length: float) -> None:
        self.pos, self.length = pos, length
        self.update()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        fm = self.fontMetrics()
        left = fmt_duration(self.pos) if self.length else "–:––"
        right = fmt_duration(self.length) if self.length else "–:––"
        lw = fm.horizontalAdvance("00:00:00") if self.length >= 3600 else fm.horizontalAdvance("00:00")
        p.setPen(QColor(DIM))
        p.drawText(QRectF(0, 0, lw, self.height()), Qt.AlignVCenter | Qt.AlignLeft, left)
        p.drawText(QRectF(self.width() - lw, 0, lw, self.height()), Qt.AlignVCenter | Qt.AlignRight, right)
        bar = QRectF(lw + 12, self.height() / 2 - 3, self.width() - 2 * lw - 24, 6)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(SURFACE2))
        p.drawRoundedRect(bar, 3, 3)
        if self.length:
            frac = max(0.0, min(1.0, self.pos / self.length))
            g = QLinearGradient(bar.topLeft(), bar.topRight())
            g.setColorAt(0, QColor("#ffc640"))
            g.setColorAt(1, QColor(ORANGE))
            p.setBrush(g)
            p.drawRoundedRect(QRectF(bar.x(), bar.y(), bar.width() * frac, bar.height()), 3, 3)


ICONS = {
    "recording": ("●", REC, "recording"),
    "partial": ("◌", DIM, ""),
    "encoding": ("◔", AMBER, "saving…"),
    "saved": ("✓", AMBER, ""),
    "exists": ("↷", DIM, "already saved"),
    "skipped": ("·", DIM, ""),
    "failed": ("✕", REC, ""),
}
ENTRY_ROLE = Qt.UserRole + 1


class LogDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index) -> QSize:
        return QSize(0, 46)

    def paint(self, p: QPainter, option, index) -> None:
        e: Entry = index.data(ENTRY_ROLE)
        if e is None:
            return
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(option.rect).adjusted(2, 2, -2, -2)
        if option.state & QStyle.State_Selected or option.state & QStyle.State_MouseOver:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(SURFACE2))
            p.drawRoundedRect(r, 8, 8)
        icon, color, default_note = ICONS.get(e.state, ("·", DIM, ""))
        f = QFont(option.font)
        f.setPixelSize(16)
        p.setFont(f)
        p.setPen(QColor(color))
        p.drawText(QRectF(r.x() + 6, r.y(), 22, r.height()), Qt.AlignCenter, icon)

        note = default_note
        if e.state == "saved":
            note = fmt_duration(e.duration)
        elif e.reason and e.state in ("partial", "skipped", "failed", "exists"):
            note = e.reason
        fn = QFont(option.font)
        fn.setPixelSize(12)
        fmn = QFontMetrics(fn)
        note_w = min(fmn.horizontalAdvance(note) + 8, r.width() * 0.4)

        tx = r.x() + 36
        tw = r.width() - 36 - note_w - 8
        ft = QFont(option.font)
        ft.setPixelSize(13)
        ft.setWeight(QFont.DemiBold)
        fmt_ = QFontMetrics(ft)
        p.setFont(ft)
        bright = e.state in ("saved", "encoding", "recording")
        p.setPen(QColor(CREAM if bright else DIM))
        p.drawText(QRectF(tx, r.y() + 5, tw, 18), Qt.AlignLeft | Qt.AlignVCenter,
                   fmt_.elidedText(e.title, Qt.ElideRight, int(tw)))
        fs = QFont(option.font)
        fs.setPixelSize(12)
        p.setFont(fs)
        p.setPen(QColor(DIM))
        p.drawText(QRectF(tx, r.y() + 23, tw, 16), Qt.AlignLeft | Qt.AlignVCenter,
                   QFontMetrics(fs).elidedText(e.subtitle, Qt.ElideRight, int(tw)))
        p.setFont(fn)
        p.setPen(QColor(AMBER if e.state in ("saved", "encoding") else color if e.state == "failed" else DIM))
        p.drawText(QRectF(r.right() - note_w - 6, r.y(), note_w, r.height()), Qt.AlignRight | Qt.AlignVCenter,
                   fmn.elidedText(note, Qt.ElideRight, int(note_w)))
        p.restore()


class ElidedLabel(QLabel):
    """Label that shortens its text with "…" instead of cutting it off."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._full = ""
        self.setObjectName("dim")
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

    def setText(self, text: str) -> None:  # noqa: N802 (Qt naming)
        self._full = text
        self._refresh()

    def text(self) -> str:
        return self._full

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._refresh()

    def _refresh(self) -> None:
        super().setText(self.fontMetrics().elidedText(self._full, Qt.ElideMiddle, max(10, self.width())))


class LogList(QListWidget):
    """Log of recorded tracks, with a hint while it is still empty."""

    HINT = "Recorded tracks show up here"

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if self.count() == 0:
            p = QPainter(self.viewport())
            p.setPen(QColor(DIM))
            p.drawText(self.viewport().rect(), Qt.AlignCenter, self.HINT)
            p.end()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(QIcon(str(ICON_SRC)))
        self.settings = QSettings("Reelcast", "Reelcast")
        self.bridge = Bridge()
        self.cancel: CancelToken | None = None
        self.worker: threading.Thread | None = None
        self.mode = ""  # "" | "auto" | "link"
        self.status_code = "idle"
        self.items: dict[int, QListWidgetItem] = {}
        self._cover_for = ""
        self._covers: dict[str, QPixmap] = {}

        root = QWidget()
        self.setCentralWidget(root)
        lay = QVBoxLayout(root)
        lay.setContentsMargins(22, 18, 22, 18)
        lay.setSpacing(14)

        # Header
        head = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(QIcon(str(ICON_SRC)).pixmap(44, 44))
        head.addWidget(logo)
        names = QVBoxLayout()
        names.setSpacing(0)
        word = QLabel("REELCAST")
        wf = QFont(self.font())
        wf.setPixelSize(22)
        wf.setWeight(QFont.Black)
        wf.setLetterSpacing(QFont.AbsoluteSpacing, 4)
        word.setFont(wf)
        word.setStyleSheet(f"color: {AMBER};")
        names.addWidget(word)
        sub = QLabel("Record Spotify, track by track")
        sub.setObjectName("dim")
        names.addWidget(sub)
        head.addLayout(names)
        head.addStretch()
        self.auto = Toggle("Auto Record")
        self.auto.setToolTip("Record everything Spotify plays on this computer, one file per track")
        self.auto.toggled.connect(self.on_auto)
        head.addWidget(self.auto)
        lay.addLayout(head)

        # Cassette + progress
        self.cassette = Cassette()
        lay.addWidget(self.cassette, 5)
        self.progress = Progress()
        lay.addWidget(self.progress)
        self.status = QLabel("Ready")
        self.status.setObjectName("status")
        self.status.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.status)

        # Log
        self.log = LogList()
        self.log.setItemDelegate(LogDelegate(self.log))
        self.log.setMouseTracking(True)
        self.log.setFocusPolicy(Qt.NoFocus)
        self.log.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.log.setMinimumHeight(150)
        self.log.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        self.log.itemDoubleClicked.connect(self.reveal)
        self.log.setToolTip("Double-click to show the file in its folder")
        lay.addWidget(self.log, 4)

        # Single episode via link
        row = QHBoxLayout()
        self.link = QLineEdit()
        self.link.setPlaceholderText("Paste a Spotify episode link")
        self.link.setToolTip("For example https://open.spotify.com/episode/…")
        self.link.returnPressed.connect(self.on_link)
        row.addWidget(self.link, 1)
        self.go = QPushButton("Record")
        self.go.setObjectName("primary")
        self.go.setCursor(Qt.PointingHandCursor)
        self.go.clicked.connect(self.on_link)
        row.addWidget(self.go)
        lay.addLayout(row)

        # Footer
        foot = QHBoxLayout()
        self.out_label = ElidedLabel()
        foot.addWidget(self.out_label, 1)
        choose = QPushButton("Folder…")
        choose.setToolTip("Choose where recordings are saved")
        choose.clicked.connect(self.choose_dir)
        foot.addWidget(choose)
        openb = QPushButton("Open")
        openb.setToolTip("Open the output folder")
        openb.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.out_dir))))
        foot.addWidget(openb)
        self.fmt = QComboBox()
        for f in ("mp3", "m4a", "opus", "flac"):
            self.fmt.addItem(f.upper(), f)
        self.fmt.setCurrentIndex(max(0, self.fmt.findData(self.settings.value("format", "mp3"))))
        self.fmt.currentIndexChanged.connect(lambda: self.settings.setValue("format", self.fmt.currentData()))
        foot.addWidget(self.fmt)
        self.listen = QCheckBox("Listen")
        self.listen.setToolTip("Hear what is being recorded through your speakers")
        self.listen.setChecked(self.settings.value("listen", True, type=bool))
        self.listen.toggled.connect(lambda v: self.settings.setValue("listen", v))
        foot.addWidget(self.listen)
        lay.addLayout(foot)

        self.out_dir = Path(self.settings.value("out_dir", str(default_out_dir())))
        self._show_out_dir()

        b = self.bridge
        b.status.connect(self.on_status)
        b.phase.connect(self.on_phase)
        b.now_playing.connect(self.on_now_playing)
        b.position.connect(self.progress.set)
        b.position.connect(self.on_position)
        b.entry.connect(self.on_entry)
        b.level.connect(self.cassette.add_level)
        b.message.connect(self.on_message)
        b.finished.connect(self.on_finished)
        self.setMinimumWidth(640)
        self.resize(720, 860)
        QTimer.singleShot(0, self.link.setFocus)

    # ---- settings -----------------------------------------------------------

    def _show_out_dir(self) -> None:
        home = str(Path.home())
        shown = str(self.out_dir).replace(home, "~", 1) if str(self.out_dir).startswith(home) else str(self.out_dir)
        self.out_label.setText(f"Saving to {shown}")
        self.out_label.setToolTip(str(self.out_dir))

    def choose_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Choose output folder", str(self.out_dir))
        if d:
            self.out_dir = Path(d)
            self.settings.setValue("out_dir", d)
            self._show_out_dir()

    def options(self) -> Options:
        return Options(out_dir=self.out_dir, fmt=self.fmt.currentData(), listen=self.listen.isChecked())

    def _busy(self, mode: str) -> None:
        self.mode = mode
        self.link.setEnabled(not mode)
        self.go.setEnabled(not mode)
        self.fmt.setEnabled(not mode)
        self.listen.setEnabled(not mode)
        self.auto.setEnabled(mode in ("", "auto"))

    # ---- start / stop --------------------------------------------------------

    def on_auto(self, on: bool) -> None:
        if on and not self.mode:
            self.cancel = CancelToken()
            session = AutoSession(self.options(), QtSessionReporter(self.bridge), self.cancel)
            self._run("auto", session.run)
        elif not on and self.mode == "auto" and self.cancel:
            self.cancel.cancel()
            self.on_phase("Stopping – saving what's left…")
            self.auto.setEnabled(False)

    def on_link(self) -> None:
        text = self.link.text().strip()
        if not text or self.mode:
            return
        self.cancel = CancelToken()
        entry = Entry(-int(time.time() * 1000), "episode", "Loading episode…", text, 0.0)
        rep = QtEpisodeReporter(self.bridge, entry)
        engine = Engine(self.options(), rep, self.cancel)

        def job():
            try:
                res = engine.run(text)
                entry.path, entry.duration = res.path, res.duration
                entry.state = "exists" if res.skipped else "saved"
                entry.reason = "already saved" if res.skipped else ""
            except Cancelled:
                entry.state, entry.reason = "skipped", "cancelled"
                raise
            except Exception as e:
                entry.state, entry.reason = "failed", str(e)
                raise
            finally:
                self.bridge.entry.emit(entry)
                engine.close()

        self.link.clear()
        self._run("link", job)

    def _run(self, mode: str, fn) -> None:
        self._busy(mode)

        def wrapper():
            err = None
            try:
                fn()
            except Cancelled:
                pass
            except ReelcastError as e:
                err = str(e)
            except Exception as e:  # pragma: no cover
                err = f"{type(e).__name__}: {e}"
            self.bridge.finished.emit(err)

        self.worker = threading.Thread(target=wrapper, name=mode, daemon=True)
        self.worker.start()

    def on_finished(self, err) -> None:
        self.worker = None
        self._busy("")
        self.auto.blockSignals(True)
        self.auto.setChecked(False)
        self.auto.blockSignals(False)
        self.on_now_playing(None)
        self.on_status("error" if err else "idle")
        if err:
            QMessageBox.warning(self, APP_NAME, err)

    # ---- display -----------------------------------------------------------------

    def on_status(self, code: str) -> None:
        self.status_code = code
        self.status.setText(STATUS_TEXT.get(code, code))
        self.cassette.paused = code == sess.PAUSED
        cur = getattr(self, "_current", None)
        self.cassette.recording = code == sess.RECORDING and cur is not None and cur.state == "recording"

    def on_phase(self, text: str) -> None:
        self.status_code = "busy"
        self.status.setText(text)

    def on_position(self, pos: float, length: float) -> None:
        self.cassette.progress = pos / length if length else 0.0

    def on_now_playing(self, entry: Entry | None) -> None:
        self._current = entry
        if entry is None:
            self.cassette.title = "Ready"
            self.cassette.subtitle = "Play something in Spotify" if self.mode == "auto" else "Turn on Auto Record or paste a link"
            self.cassette.cover = None
            self.cassette.recording = False
            self.cassette.progress = 0.0
            self.cassette.levels.clear()
            self.progress.set(0, 0)
            return
        self.cassette.title = entry.title
        self.cassette.subtitle = entry.subtitle
        self.cassette.recording = entry.state == "recording"
        self.cassette.progress = 0.0
        self.progress.set(0, entry.length)
        self._load_cover(entry)

    def _load_cover(self, entry: Entry) -> None:
        key = entry.art_url or f"{entry.artist}|{entry.album}|{entry.title}"
        self._cover_for = key
        if key in self._covers:
            self.cassette.cover = self._covers[key]
            return
        self.cassette.cover = None

        def fetch():
            data = encode.fetch_cover(entry.art_url)
            if not data:
                if entry.kind == "episode":
                    data = encode.fetch_cover(artwork.show_artwork_url(entry.album or entry.subtitle))
                else:
                    data = encode.fetch_cover(artwork.track_artwork_url(entry.artist, entry.title, entry.album))
            if data:
                QTimer.singleShot(0, self, lambda: self._set_cover(key, data))

        threading.Thread(target=fetch, daemon=True).start()

    def _set_cover(self, key: str, data: bytes) -> None:
        pm = QPixmap()
        pm.loadFromData(data)
        self._covers[key] = pm
        if self._cover_for == key:
            self.cassette.cover = pm

    def on_entry(self, entry: Entry) -> None:
        item = self.items.get(entry.key)
        if entry.state == "dropped":
            if item:
                self.log.takeItem(self.log.row(item))
                del self.items[entry.key]
            return
        if item is None:
            item = QListWidgetItem()
            self.items[entry.key] = item
            self.log.insertItem(0, item)
        item.setData(ENTRY_ROLE, entry)
        item.setToolTip(str(entry.path) if entry.path else entry.reason)
        self.log.viewport().update()
        if entry is getattr(self, "_current", None):
            self.cassette.recording = entry.state == "recording" and self.status_code == sess.RECORDING

    def on_message(self, text: str, warning: bool) -> None:
        self.status.setText(("⚠ " if warning else "") + text)
        if warning:
            self.status.setStyleSheet(f"color: {AMBER};")
            QTimer.singleShot(8000, lambda: self.status.setStyleSheet(""))

    def reveal(self, item: QListWidgetItem) -> None:
        e: Entry = item.data(ENTRY_ROLE)
        if e and e.path and e.path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(e.path.parent)))

    def closeEvent(self, event) -> None:
        if self.worker and self.worker.is_alive():
            if QMessageBox.question(self, APP_NAME, "Reelcast is recording. Quit anyway?\n"
                                    "The track that is playing right now won't be saved.") != QMessageBox.Yes:
                event.ignore()
                return
            if self.cancel:
                self.cancel.cancel()
            self.worker.join(20)
        event.accept()


def main(auto: bool = False) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setDesktopFileName("reelcast")
    app.setWindowIcon(QIcon(str(ICON_SRC)))
    app.setStyleSheet(STYLE)
    win = MainWindow()
    win.show()
    if auto:
        QTimer.singleShot(300, lambda: win.auto.setChecked(True))
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
