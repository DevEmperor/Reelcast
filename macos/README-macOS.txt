Reelcast for macOS
==================

Records podcasts and music from Spotify, one file per track.

Requirements
  - macOS 14.2 (Sonoma) or newer
  - The Spotify desktop app, logged in

Install
  1. Drag "Reelcast" onto "Applications".
  2. The first time, right-click Reelcast in Applications and choose "Open",
     then "Open" again. (Reelcast isn't signed with a paid Apple developer
     certificate, so macOS asks once.) If macOS refuses: System Settings →
     Privacy & Security → scroll down → "Open Anyway".

First recording
  macOS asks twice – both are needed:
  - "Reelcast wants access to control Spotify"  → Allow
  - "Reelcast would like to record audio"       → Allow
  You can change both later under System Settings → Privacy & Security
  (Automation, and Screen & System Audio Recording).

Using it
  - Auto Record: switch it on and play anything in Spotify. Every complete
    track is saved; skipped tracks and ads are not.
  - Single episode: paste a Spotify episode link and press Record.
  - Recordings go to ~/Music/Reelcast (Music/<artist>/<album>/ and
    Podcasts/<show>/).

Good to know
  - Spotify stays at 100 % volume while recording; use the system volume.
  - Turn off Crossfade and Automix in Spotify's settings for clean cuts.
  - Podcasts must play at 1x speed.
  - On Apple silicon Macs, Reelcast runs through Rosetta 2 (macOS offers to
    install it if needed).

Third-party software
  Reelcast includes FFmpeg (LGPL 2.1, https://ffmpeg.org) built with LAME
  (LGPL, https://lame.sourceforge.io), Qt / PySide6 (LGPL 3) and Python (PSF).
  Source code of these components is available from their websites; the
  exact build settings are documented in the Reelcast source.
