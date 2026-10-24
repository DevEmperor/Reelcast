# Reelcast

Record podcasts and music from Spotify, track by track – on Linux (PipeWire),
with macOS in progress.

Spotify plays into its own virtual output, and Reelcast records exactly that
stream – no detour through speakers or a microphone. Other apps and system
sounds never end up in a recording. Nothing is decrypted or bypassed: Reelcast
records what Spotify plays anyway.

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

Without installing, `python3 -m reelcast …` works from the project folder.
`./install.sh` adds the `reelcast` command and a menu entry,
`./install.sh --remove` takes both away again.

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
  they were, and the virtual output is gone.

## Requirements (Linux)

PipeWire with `pactl`, `pw-record`, `pw-metadata`; `ffmpeg` with libmp3lame;
Python ≥ 3.11 with `rich`, `numpy`, PyGObject (`gi`) and, for the interface,
PySide6; Spotify as a Flatpak or native package.

## Tests

`tests/run_auto_test.py` checks Auto Record against a fake Spotify player
(MPRIS + test tones): cut accuracy, pauses, ads, skipped tracks. The test is
silent.
