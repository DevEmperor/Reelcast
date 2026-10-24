# PyInstaller spec for Reelcast.app (macOS). Build with macos/build-app.sh.
import os
import sys

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
sys.path.insert(0, ROOT)
from reelcast import __version__  # noqa: E402

FF = os.path.expanduser("~/reelcast-dev/ffbuild")

a = Analysis(
    [os.path.join(SPECPATH, "reelcast_app.py")],
    pathex=[ROOT],
    binaries=[
        (os.path.join(SPECPATH, "build", "reelcast-helper"), "bin"),
        (os.path.join(FF, "ffmpeg"), "bin"),
        (os.path.join(FF, "ffprobe"), "bin"),
    ],
    datas=[(os.path.join(ROOT, "reelcast", "assets", "reelcast.svg"), "reelcast/assets")],
    hiddenimports=["reelcast.macbackend", "reelcast.selftest", "PySide6.QtSvg"],
    excludes=["gi", "tkinter", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
              "PySide6.QtQml", "PySide6.QtQuick", "PySide6.Qt3DCore", "PySide6.QtMultimedia"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Reelcast", console=False,
          argv_emulation=False, target_arch=None, codesign_identity=None)
coll = COLLECT(exe, a.binaries, a.datas, name="Reelcast")
app = BUNDLE(
    coll,
    name="Reelcast.app",
    icon=os.path.join(SPECPATH, "build", "Reelcast.icns"),
    bundle_identifier="net.devemperor.reelcast",
    version=__version__,
    info_plist={
        "CFBundleName": "Reelcast",
        "CFBundleDisplayName": "Reelcast",
        "CFBundleShortVersionString": __version__,
        "CFBundleVersion": __version__,
        "LSMinimumSystemVersion": "14.2",
        "NSHighResolutionCapable": True,
        "NSAudioCaptureUsageDescription": "Reelcast records the audio that Spotify plays – nothing else.",
        "NSAppleEventsUsageDescription": "Reelcast reads what Spotify is playing and controls playback.",
        "LSApplicationCategoryType": "public.app-category.music",
    },
)
