# Reelcast

Podcasts und Musik von Spotify titelgenau mitschneiden – unter Linux (PipeWire),
macOS in Arbeit.

Spotify spielt auf einem eigenen, virtuellen Ausgang. Reelcast nimmt genau diesen
Strom auf, ohne Umweg über Lautsprecher oder Mikrofon. Andere Programme und
Systemklänge landen nie in der Aufnahme. Nichts wird entschlüsselt oder umgangen:
aufgenommen wird, was Spotify ohnehin abspielt.

## Zwei Arten aufzunehmen

**Auto-Mitschnitt** – Reelcast läuft im Hintergrund und schneidet alles mit, was
du in Spotify auf diesem Rechner abspielst. Jeder Titel wird eine eigene Datei.

- Die Schnitte kommen aus Spotifys Position und sitzen auf wenige Millisekunden
  genau. Liegt an der Grenze Stille, wird genau dort geschnitten.
- Pausen und Puffern werden entfernt. Gewollte Stille in Stücken bleibt erhalten.
- Gespeichert wird nur, was vollständig lief. Übersprungene oder gespulte Titel,
  Werbung und lokale Dateien werden verworfen.
- Podcasts müssen in Spotify mit **1×** laufen. Andere Tempi werden erkannt und
  nicht gespeichert.

**Einzelne Folge** – Spotify-Folgenlink angeben. Reelcast startet Spotify bei
Bedarf (minimiert), spielt die Folge stumm ab, nimmt sie auf und räumt danach auf.

## Bedienen

```bash
reelcast --gui                                  # Oberfläche
reelcast --auto                                 # Auto-Mitschnitt im Terminal (Strg+C beendet)
reelcast 'https://open.spotify.com/episode/…'   # einzelne Folge
reelcast --pruefen                              # Systemcheck
```

Ohne Einrichtung geht es auch mit `python3 -m reelcast …` im Projektordner.
`./install.sh` legt den Befehl `reelcast` und einen Startmenü-Eintrag an,
`./install.sh --entfernen` nimmt beides wieder weg.

## Ablage

```
~/Music/Reelcast/
├── Musik/<Interpret>/<Album>/01 - <Titel>.mp3
└── Podcasts/<Show>/<Folgentitel>.mp3
```

Mit Tags (Titel, Interpret, Album, Tracknummer, Datum) und Cover. Der
Reelcast-Ordner bekommt das Kassetten-Icon, Album- und Show-Ordner ihr Cover
(`cover.jpg`, auch als Ordnersymbol in Dolphin).

## Gut zu wissen

- **Lautstärke:** Spotify bleibt während der Aufnahme auf 100 %. Die Lautstärke
  zum Mithören regelst du am System.
- **Crossfade/Automix** in Spotify ausschalten, sonst lassen sich Übergänge
  nicht sauber trennen. `reelcast --pruefen` zeigt das an.
- **Spotify Connect:** Aufnehmen lässt sich nur, was auf diesem Rechner spielt.
- **Danach:** Spotify-Strom, Lautstärke und PipeWire-Einstellungen sind wieder
  wie vorher, der virtuelle Ausgang ist weg.

## Voraussetzungen (Linux)

PipeWire mit `pactl`, `pw-record`, `pw-metadata`; `ffmpeg` mit libmp3lame;
Python ≥ 3.11 mit `rich`, `numpy`, PyGObject (`gi`) und für die Oberfläche
PySide6; Spotify als Flatpak oder nativ.

## Tests

`tests/run_auto_test.py` prüft den Auto-Mitschnitt gegen einen nachgebauten
Spotify-Player (MPRIS + Testtöne): Schnittgenauigkeit, Pausen, Werbung,
übersprungene Titel. Der Test ist nicht zu hören.
