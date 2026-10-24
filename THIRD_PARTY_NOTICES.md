# Third-party software

Reelcast itself is MIT licensed (see [LICENSE](LICENSE)). The Linux packages contain only
Reelcast's own code and use the libraries installed on your system. The macOS app
(`Reelcast.app` in the DMG) bundles the following components, each under its own license:

| Component | License | Source |
|---|---|---|
| FFmpeg 7.1.1 (`ffmpeg`, `ffprobe`) | [LGPL 2.1](licenses/LGPL-2.1.txt) or later | <https://ffmpeg.org/releases/ffmpeg-7.1.1.tar.xz> |
| LAME 3.100 (inside the FFmpeg executables) | [LGPL 2.0](licenses/LGPL-2.0.txt) or later | <https://downloads.sourceforge.net/project/lame/lame/3.100/lame-3.100.tar.gz> |
| Qt 6 via PySide6 / Shiboken6 | [LGPL 3.0](licenses/LGPL-3.0.txt) (with [GPL 3.0](licenses/GPL-3.0.txt)) | <https://download.qt.io/official_releases/QtForPython/> |
| Python 3.13 | [PSF License 2.0](licenses/PSF-2.0.txt) | <https://www.python.org/downloads/source/> |
| NumPy | BSD 3-Clause and others (included in the app) | <https://github.com/numpy/numpy> |
| Rich | [MIT](licenses/Rich-MIT.txt) | <https://github.com/Textualize/rich> |
| PyInstaller bootloader | [GPL 2.0](licenses/GPL-2.0.txt) with bootloader exception | <https://github.com/pyinstaller/pyinstaller> |

**FFmpeg and LAME** are built without any GPL or non-free parts by
[`macos/build-ffmpeg-mac.sh`](macos/build-ffmpeg-mac.sh); that script downloads the exact
sources listed above and contains the complete configuration, so the binaries can be
rebuilt and replaced. They live as separate programs in
`Reelcast.app/Contents/Frameworks/bin/`.

**Qt / PySide6** are included as shared libraries in `Reelcast.app/Contents/Frameworks/`
and can be replaced with other builds of the same version.

The full license texts are in [`licenses/`](licenses/). They are attached to every
release and, from the next macOS build on, also part of the DMG.

Spotify is a trademark of Spotify AB. Reelcast is an independent project and is not
affiliated with, endorsed or sponsored by Spotify.
