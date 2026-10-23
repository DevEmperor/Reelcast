"""Runs single-episode jobs (used by CLI and GUI)."""

from __future__ import annotations

from pathlib import Path

from . import artwork, encode, paths, recorder
from .model import (
    PHASE_ENCODING,
    PHASE_RESOLVE,
    CancelToken,
    Episode,
    Options,
    ReelcastError,
    Reporter,
    Result,
    fmt_duration,
)
from .paths import default_out_dir  # noqa: F401  (used by CLI/GUI)
from .player import Spotify
from .spotify_meta import fetch_episode, parse_episode_id


class Engine:
    def __init__(self, opts: Options, reporter: Reporter, cancel: CancelToken | None = None):
        self.opts = opts
        self.reporter = reporter
        self.cancel = cancel or CancelToken()
        self._spotify: Spotify | None = None

    # ---- public ---------------------------------------------------------------

    def resolve(self, link: str) -> Episode:
        eid = parse_episode_id(link)
        self.reporter.phase(PHASE_RESOLVE)
        ep = fetch_episode(eid)
        if not ep.title:
            raise ReelcastError("Spotify returns no title for this episode.")
        self.reporter.episode(ep)
        return ep

    def target_path(self, ep: Episode) -> Path:
        return paths.episode_path(self.opts.out_dir, ep.show, ep.title, self.opts.fmt)

    def run(self, link: str) -> Result:
        ep = self.resolve(link)
        dest = self.target_path(ep)
        if dest.exists() and not self.opts.overwrite:
            dur, _ = encode.probe(dest)
            return Result(dest, "existing", dur, skipped=True, note="already exists")
        dest.parent.mkdir(parents=True, exist_ok=True)
        cover = encode.fetch_cover(ep.cover_url) or encode.fetch_cover(artwork.show_artwork_url(ep.show))
        paths.decorate(self.opts.out_dir, dest.parent, cover)

        if not ep.playable:
            raise ReelcastError("Spotify reports this episode as not playable.")
        return self._from_spotify(ep, dest, cover)

    def close(self) -> None:
        """After all jobs: close Spotify again if we started it."""
        if self._spotify:
            try:
                self._spotify.quit()
            finally:
                self._spotify.close()
                self._spotify = None

    # ---- recording ------------------------------------------------------------

    def _from_spotify(self, ep: Episode, dest: Path, cover) -> Result:
        if self._spotify is None:
            self._spotify = Spotify()
        flac = dest.with_name(f".reelcast-{ep.id}.flac")
        try:
            rec = recorder.record(self._spotify, ep, self.opts, self.reporter, self.cancel, flac)
            self.reporter.phase(PHASE_ENCODING)
            encode.finalize(
                flac, dest, encode.episode_tags(ep, "Recorded from Spotify"), self.opts.fmt,
                self.opts.quality, cover, self.reporter, self.cancel, segments=rec.segments,
            )
        finally:
            flac.unlink(missing_ok=True)
        dur, _ = encode.probe(dest)
        note = ""
        if dur < rec.expected - 5:
            note = f"shorter than expected ({fmt_duration(dur)} of {fmt_duration(rec.expected)})"
            self.reporter.warn(f"Recording is {note}.")
        return Result(dest, "spotify", dur, note=note or "recorded")
