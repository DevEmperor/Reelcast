#!/bin/bash
# Build the Linux deliverables into dist/:
#   reelcast-<ver>-1-any.pkg.tar.zst   (Arch/Manjaro: sudo pacman -U …)
#   reelcast-<ver>-linux.tar.gz        (any distro: unpack, ./install.sh)
set -euo pipefail
cd "$(dirname "$0")/.."
VER=$(python3 -c "import reelcast; print(reelcast.__version__)")
mkdir -p dist
rm -f dist/reelcast-$VER*.{tar.gz,pkg.tar.zst}
git archive --format=tar.gz --prefix="reelcast-$VER/" -o "dist/reelcast-$VER-linux.tar.gz" HEAD \
    reelcast install.sh README.md
WORK=$(mktemp -d)
cp packaging/arch/PKGBUILD "$WORK/"
cp "dist/reelcast-$VER-linux.tar.gz" "$WORK/reelcast-$VER.tar.gz"
( cd "$WORK" && makepkg -f --nodeps --noconfirm >/dev/null )
cp "$WORK"/reelcast-$VER-*.pkg.tar.zst dist/
rm -rf "$WORK"
( cd dist && sha256sum reelcast-$VER* > SHA256SUMS-linux.txt && cat SHA256SUMS-linux.txt && du -h reelcast-$VER* )
