#!/bin/sh
# Reelcast für den aktuellen Benutzer einrichten: Befehl `reelcast`, Startmenü-Eintrag, Icon.
# Rückgängig: ./install.sh --entfernen
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
BIN="$HOME/.local/bin/reelcast"
APP="$HOME/.local/share/applications/reelcast.desktop"
ICON="$HOME/.local/share/icons/hicolor/scalable/apps/reelcast.svg"

if [ "${1:-}" = "--entfernen" ]; then
    rm -f "$BIN" "$APP" "$ICON"
    echo "Reelcast-Starter entfernt (Aufnahmen bleiben unangetastet)."
    exit 0
fi

mkdir -p "$(dirname "$BIN")" "$(dirname "$APP")" "$(dirname "$ICON")"
cat > "$BIN" <<EOF
#!/bin/sh
exec python3 -c 'import sys; sys.path.insert(0, "$HERE"); from reelcast.cli import main; sys.exit(main())' "\$@"
EOF
chmod +x "$BIN"
command cp -f "$HERE/reelcast/assets/reelcast.svg" "$ICON"
cat > "$APP" <<EOF
[Desktop Entry]
Type=Application
Name=Reelcast
GenericName=Spotify-Mitschnitt
Comment=Podcasts und Musik von Spotify titelgenau mitschneiden
Exec=$BIN --gui
Icon=reelcast
Terminal=false
Categories=AudioVideo;Audio;Recorder;
StartupWMClass=reelcast
EOF
echo "Eingerichtet: Befehl 'reelcast' und Startmenü-Eintrag 'Reelcast'."
