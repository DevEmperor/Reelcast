"""Cover-Ersatz aus Apple Podcasts.

Nur das Bild – die Audiodaten kommen immer von Spotify. Gebraucht, wenn Spotifys
Bildserver nicht erreichbar sind (z. B. von einem DNS-Filter blockiert).
"""

from __future__ import annotations

import re
import unicodedata
import urllib.parse

from . import net


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"\s+", " ", re.sub(r"[^\w]+", " ", text)).strip()


def show_artwork_url(show: str) -> str:
    if not show:
        return ""
    url = "https://itunes.apple.com/search?" + urllib.parse.urlencode(
        {"media": "podcast", "entity": "podcast", "limit": 10, "term": show}
    )
    try:
        results = net.get_json(url).get("results", [])
    except Exception:
        return ""
    for r in results:
        if _norm(r.get("collectionName", "")) == _norm(show):
            art = r.get("artworkUrl600") or r.get("artworkUrl100") or ""
            return art.replace("100x100bb", "600x600bb")
    return ""


def track_artwork_url(artist: str, title: str, album: str) -> str:
    """Albumcover aus dem Apple-Katalog (nur das Bild)."""
    if not (artist and (album or title)):
        return ""
    url = "https://itunes.apple.com/search?" + urllib.parse.urlencode(
        {"media": "music", "entity": "song", "limit": 15, "term": f"{artist} {album or title}"}
    )
    try:
        results = net.get_json(url).get("results", [])
    except Exception:
        return ""
    first_artist = artist.split(",")[0]
    for r in results:
        if _norm(r.get("artistName", "")).startswith(_norm(first_artist)) and (
            not album or _norm(r.get("collectionName", "")) == _norm(album)
        ):
            return (r.get("artworkUrl100") or "").replace("100x100bb", "600x600bb")
    return ""
