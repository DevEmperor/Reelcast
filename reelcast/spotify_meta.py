"""Spotify link → episode metadata, without a Spotify account or API key.

The source is the public embed page (the one Spotify serves to embedded players);
oEmbed is the fallback.
"""

from __future__ import annotations

import html
import json
import re
import time
import urllib.error
import urllib.parse

from . import net
from .model import Episode, ReelcastError

_ID = r"([A-Za-z0-9]{22})"
_PATTERNS = [
    re.compile(r"^spotify:episode:" + _ID + r"$"),
    re.compile(r"open\.spotify\.com/(?:intl-[A-Za-z-]+/)?(?:embed/)?episode/" + _ID),
    re.compile(r"^" + _ID + r"$"),
]
_NEXT = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)


def parse_episode_id(text: str) -> str:
    text = text.strip()
    for pat in _PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(1)
    if re.search(r"open\.spotify\.com/(?:intl-[A-Za-z-]+/)?(track|album|playlist|show)/", text):
        kind = re.search(r"/(track|album|playlist|show)/", text).group(1)
        raise ReelcastError(
            f"That's a Spotify {kind} link. Links are for single podcast episodes "
            "(open.spotify.com/episode/…) – for music, use Auto Record."
        )
    raise ReelcastError(f"Not a Spotify episode link: {text!r}")


def fetch_episode(eid: str) -> Episode:
    try:
        return _from_embed(eid)
    except ReelcastError:
        raise
    except Exception:
        return _from_oembed(eid)


def _embed_entity(kind: str, ident: str, tries: int = 8) -> dict:
    """The entity from Spotify's embed page.

    Some of Spotify's servers answer "page not found" for pages that exist – as HTTP 404 or
    as a 404 page with status 200, for about every second request. Asking again helps.
    """
    for attempt in range(1, tries + 1):
        try:
            page = net.get_text(f"https://open.spotify.com/embed/{kind}/{ident}")
        except urllib.error.HTTPError as e:
            if e.code != 404 or attempt == tries:
                raise
        else:
            m = _NEXT.search(page)
            if not m:
                raise ValueError("embed page without __NEXT_DATA__")
            props = json.loads(m.group(1))["props"]["pageProps"]
            if "state" in props:
                return props["state"]["data"]["entity"]
            if props.get("status") != 404 or attempt == tries:
                raise LookupError(f"Spotify's embed page has no data for {kind} {ident}")
        time.sleep(0.3 * attempt)
    raise LookupError(f"Spotify's embed page has no data for {kind} {ident}")


def _from_embed(eid: str) -> Episode:
    ent = _embed_entity("episode", eid)
    if ent.get("type") not in (None, "episode"):
        raise ReelcastError("This link doesn't point to a podcast episode.")
    covers = sorted(
        ent.get("relatedEntityCoverArt") or [],
        key=lambda c: c.get("maxWidth") or 0,
        reverse=True,
    )
    release = ((ent.get("releaseDate") or {}).get("isoString") or "")[:10]
    return Episode(
        id=eid,
        title=(ent.get("name") or ent.get("title") or "").strip(),
        show=(ent.get("subtitle") or "").strip(),
        duration=(ent.get("duration") or 0) / 1000.0,
        release=release,
        cover_url=covers[0]["url"] if covers else "",
        playable=bool(ent.get("isPlayable", True)),
    )


def _from_oembed(eid: str) -> Episode:
    url = "https://open.spotify.com/oembed?url=" + urllib.parse.quote(
        f"https://open.spotify.com/episode/{eid}", safe=""
    )
    try:
        d = net.get_json(url)
    except Exception as e:
        raise ReelcastError(f"Episode {eid} not found: {e}") from e
    page = ""
    try:
        page = net.get_text(f"https://open.spotify.com/episode/{eid}")
    except Exception:
        pass

    def meta(name: str) -> str:
        m = re.search(rf'<meta (?:name|property)="{re.escape(name)}" content="([^"]*)"', page)
        return html.unescape(m.group(1)) if m else ""

    show = ""
    desc = meta("description")
    m = re.match(r"Listen to this episode from (.+?) on Spotify", desc)
    if m:
        show = m.group(1)
    dur = meta("music:duration")
    return Episode(
        id=eid,
        title=d.get("title", "").strip(),
        show=show,
        duration=float(dur) if dur.isdigit() else 0.0,
        release=meta("music:release_date")[:10],
        cover_url=d.get("thumbnail_url", ""),
    )


def fetch_track_release(track_id: str) -> str:
    """Release date (YYYY-MM-DD) of a music track, empty if unknown."""
    try:
        ent = _embed_entity("track", track_id, tries=4)
        return ((ent.get("releaseDate") or {}).get("isoString") or "")[:10]
    except Exception:
        return ""
