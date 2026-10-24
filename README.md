# Reelcast

Record podcasts and music from Spotify, track by track – on Linux (PipeWire)
and macOS (14.2 Sonoma or newer).

Reelcast records only Spotify's own audio stream – no detour through speakers
or a microphone. On Linux, Spotify plays into a virtual output of its own; on
macOS, Reelcast taps Spotify's audio directly (Core Audio process tap). Other
apps and system sounds never end up in a recording. Nothing is decrypted or
bypassed: Reelcast records what Spotify plays anyway.

## Install

| System | File | How |
|---|---|---|
| macOS 14.2+ | `Reelcast-1.0.0-macOS.dmg` | Open, drag Reelcast to Applications, see “Read me first” in the window |
| Arch / Manjaro | `reelcast-1.0.0-1-any.pkg.tar.zst` | `sudo pacman -U reelcast-1.0.0-1-any.pkg.tar.zst` |
| Other Linux | `reelcast-1.0.0-linux.tar.gz` | Unpack, then `./install.sh` (`./install.sh --remove` undoes it) |

On macOS everything is bundled (Python, Qt, FFmpeg). The first recording asks
twice – allow Reelcast to control Spotify and to record audio.

## Two ways to record

**Auto Record** – Reelcast runs in the background and records everything you
play in Spotify on this computer. Every track becomes its own file.

- Cuts come from Spotify's playback position. Spotify reports it only to about
  ±0.2 s, so Reelcast puts every cut into the silence between tracks – nothing
  of a song is cut off and nothing of the neighbouring track leaks in. On
  gapless albums (live albums, DJ mixes) a cut can be up to ~0.2 s off.
- Pauses and buffering are removed; intended silence inside a track stays.
- Only complete tracks are saved. Skipped or seeked tracks, ads and local
  files are discarded.
- Podcasts have to play at **1×** in Spotify. Other speeds are detected and
  not saved.

**Single episode** – give Reelcast a Spotify episode link. It starts Spotify if
needed (minimized), plays the episode silently, records it and cleans up.

## Usage

```bash
reelcast --gui                                  # graphical interface
reelcast --auto                                 # Auto Record in the terminal (Ctrl+C stops)
reelcast 'https://open.spotify.com/episode/…'   # single episode
reelcast --check                                # system check
```

On macOS, start Reelcast from Applications (it opens the interface). Without
installing, `python3 -m reelcast …` works from the project folder on Linux.

## Where recordings go

```
~/Music/Reelcast/
├── Music/<artist>/<album>/01 - <title>.mp3
└── Podcasts/<show>/<episode title>.mp3
```

Files are tagged (title, artist, album, track number, date) and carry cover
art. The Reelcast folder gets the cassette icon; album and show folders get
their cover (`cover.jpg`, also used as the folder icon in Dolphin).

## Good to know

- **Volume:** Spotify stays at 100 % while recording. Use your system volume
  to adjust what you hear.
- **Turn off Crossfade/Automix** in Spotify, otherwise transitions can't be
  separated cleanly. `reelcast --check` shows the setting.
- **Spotify Connect:** only what plays on this computer can be recorded.
- **Afterwards:** Spotify's stream, its volume and PipeWire are back to how
  they were, and the virtual output is gone (Linux). On macOS nothing is
  rerouted in the first place.

## Requirements (Linux)

PipeWire with `pactl`, `pw-record`, `pw-metadata`; `ffmpeg` with libmp3lame;
Python ≥ 3.11 with `rich`, `numpy`, PyGObject (`gi`) and, for the interface,
PySide6; Spotify as a Flatpak or native package.

## Requirements (macOS)

macOS 14.2 or newer and the Spotify desktop app. Building the app yourself:
`macos/build-app.sh` (Command Line Tools, Python venv, LGPL FFmpeg from
`macos/build-ffmpeg-mac.sh`); the test VM is described in
`docs/macos-test-vm.md`.

## Tests

- `tests/run_auto_test.py` checks Auto Record against a fake Spotify player
  (MPRIS + test tones): cut accuracy, pauses, ads, skipped tracks. Silent.
- `reelcast --selftest DIR` drives the real interface with the real Spotify:
  a full album track with a pause, a skipped track, a second full track, a
  stopped track and a podcast episode. It checks what was saved (length,
  tags, cover, sound), writes screenshots, waveforms and `result.json` to
  `DIR`, and deletes the recorded audio afterwards.
