"""Terminal-Oberfläche im Kassettendeck-Look (rich)."""

from __future__ import annotations

import argparse
import signal
import sys
import threading
import time
from collections import deque
from pathlib import Path

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

from . import __version__, doctor
from .engine import Engine, default_out_dir
from .model import Cancelled, CancelToken, Episode, Options, Reporter, ReelcastError, fmt_duration

console = Console(highlight=False)

AMBER = "#ffb000"
ORANGE = "#ff7a1a"
CREAM = "#f4efe6"
TAPE = "#9a6b3c"
REC = "#ff3b30"
DIM = "grey50"
DARK = "grey27"

LOGO = (
    "█▀█ █▀▀ █▀▀ █   █▀▀ ▄▀█ █▀ ▀█▀",
    "█▀▄ ██▄ ██▄ █▄▄ █▄▄ █▀█ ▄█  █ ",
)
SPOKES = "◐◓◑◒"
WAVE = " ▁▂▃▄▅▆▇█"


def _blend(c1: str, c2: str, t: float) -> str:
    a = [int(c1[i : i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i : i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(a, b))


def banner() -> Group:
    lines = []
    width = len(LOGO[0])
    for row in LOGO:
        t = Text()
        for i, ch in enumerate(row):
            t.append(ch, style=f"bold {_blend(AMBER, ORANGE, i / max(1, width - 1))}")
        lines.append(t)
    lines[0].append(f"   v{__version__}", style=DIM)
    lines[1].append("   Podcasts von Spotify mitschneiden", style=DIM)
    return Group(*lines)


def _de_date(iso: str) -> str:
    return f"{iso[8:10]}.{iso[5:7]}.{iso[0:4]}" if len(iso) >= 10 else iso


def _size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def _reel(size: int, spoke: str) -> Text:
    """Spule; `size` 0…3 = wie viel Band noch darauf ist."""
    t = Text(" " * (3 - size))
    t.append("(" * size, style=TAPE)
    t.append(spoke, style=f"bold {CREAM}")
    t.append(")" * size, style=TAPE)
    t.append(" " * (3 - size))
    return t


def cassette(progress: float, levels: deque, spinning: bool, width: int = 46) -> Group:
    """Kassette mit drehenden Spulen; dazwischen die gerade aufgenommene Welle."""
    phase = int(time.monotonic() * 8) if spinning else 0
    left = _reel(round((1 - progress) * 3), SPOKES[phase % 4])
    right = _reel(round(progress * 3), SPOKES[(phase + 2) % 4])
    foot_w = width - 2  # Innenbreite
    wave_w = foot_w - 2 * 7 - 8
    vals = list(levels)[-wave_w:]
    wave = Text()
    wave.append("·" * (wave_w - len(vals)), style=DARK)
    for db in vals:
        frac = max(0.0, min(1.0, (db + 60) / 60))
        wave.append(WAVE[round(frac * (len(WAVE) - 1))] if frac > 0 else "·", style=AMBER if frac > 0 else DARK)
    inner = Text("  ").append(left).append("  ").append(wave).append("  ").append(right).append("  ")
    edge = DARK
    top = Text("╭" + "─" * foot_w + "╮", style=edge)
    mid = Text("│", style=edge).append(inner).append("│", style=edge)
    slope = (foot_w - 24) // 2
    bottom = Text("╰" + "─" * slope + "╲" + "▁" * (foot_w - 2 * slope - 2) + "╱" + "─" * slope + "╯", style=edge)
    return Group(top, mid, bottom)


class LiveReporter(Reporter):
    def __init__(self, index: int, count: int):
        self.index, self.count = index, count
        self.ep: Episode | None = None
        self.phase_text = "Starte …"
        self.done = 0.0
        self.total: float | None = None
        self.unit = "s"
        self.levels: deque = deque(maxlen=64)
        self._acc: list[float] = []
        self.mode = ""  # "", "rec", "enc"
        self.rate_samples: list[tuple[float, float]] = []
        self.live: Live | None = None

    # ---- Reporter ------------------------------------------------------------

    def episode(self, ep: Episode) -> None:
        self.ep = ep

    def phase(self, text: str) -> None:
        self.phase_text = text
        if text.startswith("Nehme auf"):
            self.mode = "rec"
        elif text.startswith("Kodiere"):
            self.mode = "enc"
        self.done, self.total, self.rate_samples = 0.0, None, []

    def info(self, text: str) -> None:
        self._print(Text("  " + text, style=DIM))

    def warn(self, text: str) -> None:
        self._print(Text("  ▲ " + text, style=AMBER))

    def progress(self, done: float, total: float | None, unit: str = "s") -> None:
        self.done, self.total, self.unit = done, total, unit
        now = time.monotonic()
        self.rate_samples.append((now, done))
        self.rate_samples = [s for s in self.rate_samples if now - s[0] < 5]

    def level(self, peak_db: float) -> None:
        self._acc.append(peak_db)
        if len(self._acc) >= 3:  # ein Wellenbalken = 0,3 s
            self.levels.append(max(self._acc))
            self._acc = []

    def _print(self, renderable) -> None:
        (self.live.console if self.live else console).print(renderable)

    # ---- Darstellung ---------------------------------------------------------

    def _eta(self) -> str:
        if not self.total or len(self.rate_samples) < 2:
            return ""
        (t0, d0), (t1, d1) = self.rate_samples[0], self.rate_samples[-1]
        if t1 - t0 < 0.5 or d1 <= d0:
            return ""
        return "noch " + fmt_duration((self.total - self.done) / ((d1 - d0) / (t1 - t0)))

    def render(self):
        rows = []
        now = time.monotonic()
        badge = Text()
        if self.mode == "rec":
            badge.append("● " if int(now * 2) % 2 == 0 else "○ ", style=f"bold {REC}")
            badge.append("REC", style=f"bold {REC}")
            badge.append(f"  {fmt_duration(self.done)}", style=f"bold {CREAM}")
        elif self.mode == "enc":
            badge.append("◆ MP3", style=f"bold {AMBER}")
        else:
            badge.append(SPOKES[int(now * 6) % 4] + " ", style=AMBER)
        badge.append("  " + self.phase_text, style=DIM)
        if self.count > 1:
            badge.append(f"   {self.index}/{self.count}", style=DIM)
        rows.append(badge)

        if self.ep:
            rows.append(Text(self.ep.title, style=f"bold {CREAM}"))
            meta = " · ".join(
                x for x in (self.ep.show, _de_date(self.ep.release),
                            fmt_duration(self.ep.duration) if self.ep.duration else "") if x
            )
            rows.append(Text(meta, style=AMBER))

        if self.mode in ("rec", "enc"):
            frac = self.done / self.total if self.total and self.mode == "rec" else 1.0
            rows += [Text(""), cassette(max(0.0, min(1.0, frac)), self.levels, spinning=self.mode == "rec")]

        if self.total:
            frac = max(0.0, min(1.0, self.done / self.total))
            if self.unit == "B":
                label = f"{_size(self.done)} / {_size(self.total)}"
            else:
                label = f"{fmt_duration(self.done)} / {fmt_duration(self.total)}"
            line = Table.grid(padding=(0, 2))
            line.add_column()
            line.add_column(style=DIM)
            line.add_row(
                ProgressBar(total=1.0, completed=frac, width=46, complete_style=AMBER,
                            finished_style=AMBER, style=DARK),
                f"{frac * 100:3.0f} %  {label}  {self._eta()}",
            )
            rows += [Text(""), line]
        return Panel(
            Group(*rows),
            title=Text(" REELCAST ", style=f"bold black on {AMBER}"),
            title_align="left",
            border_style=AMBER,
            padding=(0, 2),
        )


def run_jobs(links: list[str], opts: Options) -> int:
    cancel = CancelToken()
    results = []
    failures = 0
    engine = None
    signal.signal(signal.SIGTERM, lambda *_: cancel.cancel())
    console.print(banner())
    console.print()
    try:
        for i, link in enumerate(links, 1):
            if cancel.cancelled:
                break
            rep = LiveReporter(i, len(links))
            if engine is None:
                engine = Engine(opts, rep, cancel)
            engine.reporter = rep
            outcome: dict = {}

            def work():
                try:
                    outcome["result"] = engine.run(link)
                except BaseException as e:  # an den Hauptthread weiterreichen
                    outcome["error"] = e

            th = threading.Thread(target=work, name="engine", daemon=True)
            with Live(get_renderable=rep.render, console=console, refresh_per_second=12, transient=True) as live:
                rep.live = live
                th.start()
                while th.is_alive():
                    try:
                        th.join(0.1)
                    except KeyboardInterrupt:
                        if not cancel.cancelled:
                            cancel.cancel()
                            rep.phase_text = "Breche ab und räume auf …"
                rep.live = None

            err = outcome.get("error")
            title = rep.ep.title if rep.ep else link
            if isinstance(err, Cancelled) or (err and cancel.cancelled):
                console.print(Text.assemble(("■ ", AMBER), ("Abgebrochen: ", "bold"), title))
                break
            if err:
                failures += 1
                msg = str(err) if isinstance(err, ReelcastError) else f"{type(err).__name__}: {err}"
                console.print(Text.assemble(("✗ ", f"bold {REC}"), (title, "bold")))
                console.print(Text("  " + msg, style=REC))
                results.append((title, None, msg))
                continue
            res = outcome["result"]
            results.append((title, res, ""))
            how = {"spotify": "von Spotify mitgeschnitten", "vorhanden": "war schon da"}[res.source]
            mark = ("↷ ", DIM) if res.skipped else ("✓ ", f"bold {AMBER}")
            console.print(Text.assemble(mark, (title, f"bold {CREAM}")))
            details = f"  {how} · {fmt_duration(res.duration)}"
            if res.note and res.source == "spotify" and res.note != "aufgenommen":
                details += f" · {res.note}"
            console.print(Text(details, style=DIM))
            console.print(Text("  " + str(res.path), style=AMBER))
    finally:
        if engine:
            engine.close()
    if len(links) > 1:
        ok = sum(1 for _, r, _ in results if r)
        console.print(Text(f"\n{ok} von {len(links)} Folgen mitgeschnitten.", style="bold"))
    return 130 if cancel.cancelled else (1 if failures else 0)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="reelcast",
        description="Reelcast – Podcast-Folgen von Spotify als MP3 mitschneiden. Spotify spielt "
        "still auf einem virtuellen Ausgang, Reelcast nimmt genau diesen Strom auf.",
    )
    p.add_argument("links", nargs="*", metavar="LINK", help="Spotify-Folgenlink(s) oder spotify:episode:…")
    p.add_argument("-l", "--liste", type=Path, help="Datei mit Links (einer pro Zeile)")
    p.add_argument("-o", "--ziel", type=Path, help=f"Zielordner (Standard: {default_out_dir()})")
    p.add_argument("-f", "--format", choices=("mp3", "m4a", "opus", "flac"), default="mp3")
    p.add_argument("-q", "--qualitaet", type=int, default=2, choices=range(10), metavar="0-9",
                   help="MP3-VBR-Stufe, 0 = beste (Standard 2 ≈ 190 kbit/s)")
    p.add_argument("--mithoeren", action="store_true", help="Aufnahme über die Lautsprecher mithören")
    p.add_argument("--ueberschreiben", action="store_true", help="vorhandene Dateien ersetzen")
    p.add_argument("--erzwingen", action="store_true", help="laufende Spotify-Wiedergabe unterbrechen")
    p.add_argument("--spotify-zeigen", action="store_true", help="selbst gestartetes Spotify nicht minimieren")
    p.add_argument("--pruefen", action="store_true", help="System prüfen (PipeWire, ffmpeg, Spotify)")
    p.add_argument("--gui", action="store_true", help="grafische Oberfläche starten")
    p.add_argument("--version", action="version", version=f"Reelcast {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.gui:
        from .gui import main as gui_main

        return gui_main()
    if args.pruefen:
        console.print(banner())
        console.print()
        return doctor.print_report(console)

    links = list(args.links)
    if args.liste:
        for line in args.liste.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                links.append(line)
    if not links:
        if not sys.stdin.isatty():
            links = [line.strip() for line in sys.stdin if line.strip()]
        else:
            console.print(banner())
            console.print()
            try:
                link = console.input(f"[{AMBER}]▸ Spotify-Folgenlink:[/] ").strip()
            except (EOFError, KeyboardInterrupt):
                return 130
            if link:
                links = [link]
    if not links:
        build_parser().print_help()
        return 2

    opts = Options(
        out_dir=(args.ziel or default_out_dir()).expanduser(),
        fmt=args.format,
        quality=args.qualitaet,
        listen=args.mithoeren,
        overwrite=args.ueberschreiben,
        force=args.erzwingen,
        show_spotify=args.spotify_zeigen,
    )
    return run_jobs(links, opts)


if __name__ == "__main__":
    sys.exit(main())
