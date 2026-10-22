"""Reelcast – Podcast-Folgen von Spotify als MP3 einfangen.

Zwei Wege, automatisch gewählt:

* **Feed** – die meisten Podcasts sind öffentlich per RSS verteilt. Findet sich
  die Folge dort, wird die Originaldatei in Sekunden geladen (kein Umkodieren
  von Spotify-Audio, volle Qualität).
* **Aufnahme** – sonst spielt Spotify die Folge auf einem virtuellen Ausgang ab
  und Reelcast nimmt genau diesen Strom auf (Echtzeit, still im Hintergrund).
"""

__version__ = "1.0.0"
APP_NAME = "Reelcast"
