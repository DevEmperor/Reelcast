# Changelog

## 1.0.0 – 2026-10-24

First public release.

- **Auto Record:** records everything Spotify plays on this computer, one file per track.
  Only complete tracks are kept; skipped or seeked tracks, ads and local files are left
  out. Cuts land in the silence between tracks, pauses and buffering are removed.
- **Single episodes:** paste a Spotify episode link – Reelcast starts Spotify if needed,
  plays the episode (silently, if you like) and saves it.
- Tags (title, artist, album, track number, date) and cover art; files sorted into
  `Music/<artist>/<album>/` and `Podcasts/<show>/`. MP3, M4A and FLAC (plus Opus on Linux).
- Qt interface with a live cassette, and a full terminal interface (`reelcast --auto`).
- **Linux:** PipeWire – Spotify plays into its own virtual output; MPRIS for the player.
- **macOS 14.2+:** Core Audio process tap and AppleScript through a small Swift helper;
  self-contained app with an LGPL build of FFmpeg/LAME.
- `reelcast --check` system check and `reelcast --selftest DIR` end-to-end test with the
  real Spotify app (screenshots, waveforms, `result.json`).
