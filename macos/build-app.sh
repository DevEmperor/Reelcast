#!/bin/bash
# Build Reelcast.app and Reelcast-<version>-macOS.dmg (run on macOS 14.2+ with the
# Command Line Tools). Expects:
#   ~/reelcast-dev/venv (Python 3.13 + PySide6-Essentials, numpy, rich, pyinstaller)
#   ~/reelcast-dev/ffbuild/{ffmpeg,ffprobe} (LGPL build from build-ffmpeg-mac.sh)
set -euo pipefail
cd "$(dirname "$0")"
ROOT=$(cd .. && pwd)
PY=~/reelcast-dev/venv/bin/python
VERSION=$($PY -c "import sys; sys.path.insert(0, '$ROOT'); import reelcast; print(reelcast.__version__)")
mkdir -p build

echo "== helper"
swiftc -O -target x86_64-apple-macos14.2 ReelcastHelper.swift -o build/reelcast-helper

echo "== icon"
rm -rf build/Reelcast.iconset && mkdir -p build/Reelcast.iconset
QT_QPA_PLATFORM=offscreen $PY - <<EOF
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtCore import Qt
app = QGuiApplication([])
r = QSvgRenderer("$ROOT/reelcast/assets/reelcast.svg")
for size in (16, 32, 128, 256, 512):
    for scale in (1, 2):
        px = size * scale
        img = QImage(px, px, QImage.Format_ARGB32)
        img.fill(Qt.transparent)
        p = QPainter(img); r.render(p); p.end()
        name = f"icon_{size}x{size}" + ("@2x" if scale == 2 else "") + ".png"
        img.save(f"build/Reelcast.iconset/{name}")
EOF
iconutil -c icns build/Reelcast.iconset -o build/Reelcast.icns

echo "== app"
rm -rf build/pyi dist
$PY -m PyInstaller --noconfirm --clean --distpath dist --workpath build/pyi Reelcast.spec
codesign --force --deep -s - dist/Reelcast.app
codesign --verify --deep --strict dist/Reelcast.app && echo "signature ok (ad-hoc)"

echo "== dmg"
STAGE=build/dmg
rm -rf "$STAGE" && mkdir -p "$STAGE"
cp -R dist/Reelcast.app "$STAGE/"
ln -s /Applications "$STAGE/Applications"
cp "$ROOT/macos/README-macOS.txt" "$STAGE/Read me first.txt"
mkdir -p "$STAGE/Licenses"
cp "$ROOT/LICENSE" "$STAGE/Licenses/Reelcast (MIT).txt"
cp "$ROOT/THIRD_PARTY_NOTICES.md" "$ROOT"/licenses/*.txt "$STAGE/Licenses/"
DMG="dist/Reelcast-$VERSION-macOS.dmg"
rm -f "$DMG"
hdiutil create -volname "Reelcast $VERSION" -srcfolder "$STAGE" -ov -format UDZO "$DMG" >/dev/null
shasum -a 256 "$DMG" | tee "$DMG.sha256"
du -sh dist/Reelcast.app "$DMG"
