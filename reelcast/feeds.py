"""Schneller Weg: die Folge im öffentlichen RSS-Feed des Podcasts finden.

Verzeichnisse: Apple Podcasts (iTunes Search API) und fyyd.de, beide ohne Schlüssel.
Eine Folge gilt nur dann als gefunden, wenn Titel *und* (soweit bekannt) Dauer
und Datum zusammenpassen – lieber aufnehmen als die falsche Datei laden.
"""

from __future__ import annotations

import re
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime

from . import net
from .model import CancelToken, Episode, Reporter

ITUNES_NS = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"


@dataclass
class FeedHit:
    audio_url: str
    title: str
    feed_url: str
    duration: float | None
    published: date | None
    mime: str
    score: float


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    text = re.sub(r"[^\w]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def similarity(a: str, b: str) -> float:
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    short, long_ = sorted((na, nb), key=len)
    if len(short) >= 12 and short in long_:
        return 0.92
    return SequenceMatcher(None, na, nb).ratio()


def parse_duration(text: str | None) -> float | None:
    if not text:
        return None
    text = text.strip()
    try:
        if ":" in text:
            secs = 0.0
            for part in text.split(":"):
                secs = secs * 60 + float(part)
            return secs
        return float(text)
    except ValueError:
        return None


def parse_date(text: str | None) -> date | None:
    if not text:
        return None
    try:
        return parsedate_to_datetime(text.strip()).date()
    except (TypeError, ValueError, IndexError):
        pass
    try:
        return datetime.fromisoformat(text.strip().replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _episode_date(ep: Episode) -> date | None:
    try:
        return date.fromisoformat(ep.release) if ep.release else None
    except ValueError:
        return None


def judge(ep: Episode, title: str, duration: float | None, published: date | None) -> float:
    """Trefferwert 0…1; unter 0.5 = kein Treffer."""
    ts = similarity(ep.title, title)
    dur_ok = None
    if duration and ep.duration:
        dur_ok = abs(duration - ep.duration) <= max(20.0, 0.05 * ep.duration)
    ep_date = _episode_date(ep)
    date_ok = None
    if published and ep_date:
        date_ok = abs((published - ep_date).days) <= 3
    if dur_ok is False and date_ok is not True:
        return 0.0
    if ts >= 0.93:
        return ts
    if ts >= 0.75 and dur_ok and date_ok is not False:
        return ts
    return 0.0


def _itunes(params: dict) -> list[dict]:
    url = "https://itunes.apple.com/search?" + urllib.parse.urlencode(params)
    try:
        return net.get_json(url).get("results", [])
    except Exception:
        return []


def _fyyd(title: str) -> list[dict]:
    url = "https://api.fyyd.de/0.2/search/podcast?" + urllib.parse.urlencode(
        {"title": title, "count": 10}
    )
    try:
        return net.get_json(url).get("data", []) or []
    except Exception:
        return []


def candidate_feeds(ep: Episode) -> tuple[list[str], list[FeedHit]]:
    """Feed-URLs des Podcasts und direkte Folgentreffer aus der Apple-Suche."""
    feeds: list[str] = []
    direct: list[FeedHit] = []

    def add(url: str | None) -> None:
        if url and url not in feeds:
            feeds.append(url)

    if ep.show:
        for r in _itunes({"media": "podcast", "entity": "podcast", "limit": 10, "term": ep.show}):
            if similarity(r.get("collectionName", ""), ep.show) >= 0.85:
                add(r.get("feedUrl"))
        for r in _fyyd(ep.show):
            if similarity(r.get("title", ""), ep.show) >= 0.85:
                add(r.get("xmlURL"))

    for r in _itunes(
        {"media": "podcast", "entity": "podcastEpisode", "limit": 25, "term": ep.title[:100]}
    ):
        if ep.show and similarity(r.get("collectionName", ""), ep.show) < 0.85:
            continue
        add(r.get("feedUrl"))
        dur = (r.get("trackTimeMillis") or 0) / 1000 or None
        pub = parse_date(r.get("releaseDate"))
        score = judge(ep, r.get("trackName", ""), dur, pub)
        if score and r.get("episodeUrl"):
            direct.append(
                FeedHit(
                    audio_url=r["episodeUrl"],
                    title=r.get("trackName", ""),
                    feed_url=r.get("feedUrl", ""),
                    duration=dur,
                    published=pub,
                    mime=r.get("episodeContentType", "") or "",
                    score=score,
                )
            )
    return feeds, direct


def search_feed(feed_url: str, ep: Episode) -> FeedHit | None:
    try:
        root = ET.fromstring(net.get_bytes(feed_url))
    except Exception:
        return None
    channel = root.find("channel")
    if channel is None:
        return None
    best: FeedHit | None = None
    for item in channel.iter("item"):
        enc = item.find("enclosure")
        if enc is None or not enc.get("url"):
            continue
        title = item.findtext("title") or item.findtext(f"{ITUNES_NS}title") or ""
        dur = parse_duration(item.findtext(f"{ITUNES_NS}duration"))
        pub = parse_date(item.findtext("pubDate"))
        score = judge(ep, title, dur, pub)
        if score and (best is None or score > best.score):
            best = FeedHit(
                audio_url=enc.get("url"),
                title=title,
                feed_url=feed_url,
                duration=dur,
                published=pub,
                mime=enc.get("type", ""),
                score=score,
            )
    return best


def find_episode(ep: Episode, reporter: Reporter, cancel: CancelToken) -> FeedHit | None:
    reporter.phase("Suche öffentlichen Podcast-Feed …")
    feeds, direct = candidate_feeds(ep)
    best = max(direct, key=lambda h: h.score, default=None)
    for url in feeds:
        cancel.check()
        if best and best.score >= 0.99:
            break
        reporter.info(f"Prüfe Feed {urllib.parse.urlsplit(url).netloc}")
        hit = search_feed(url, ep)
        if hit and (best is None or hit.score > best.score):
            best = hit
    if not feeds and not direct:
        reporter.info("Kein öffentlicher Feed zu diesem Podcast gefunden.")
    elif not best:
        reporter.info(f"{len(feeds)} Feed(s) geprüft – die Folge ist dort nicht enthalten.")
    return best
