"""Small HTTP helpers on top of the standard library."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from . import __version__
from .model import ReelcastError

UA = f"Mozilla/5.0 (X11; Linux x86_64) Reelcast/{__version__}"
TIMEOUT = 20


def _request(url: str, headers: dict | None = None) -> urllib.request.Request:
    h = {"User-Agent": UA, "Accept-Language": "en,de;q=0.8"}
    if headers:
        h.update(headers)
    return urllib.request.Request(url, headers=h)


def get_bytes(url: str, retries: int = 2, headers: dict | None = None) -> bytes:
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(_request(url, headers), timeout=TIMEOUT) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500 and e.code != 429:
                raise
            last = e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = e
        time.sleep(0.8 * (attempt + 1))
    raise ReelcastError(f"Network error at {urllib.parse.urlsplit(url).netloc}: {last}")


def get_text(url: str, **kw) -> str:
    return get_bytes(url, **kw).decode("utf-8", "replace")


def get_json(url: str, **kw):
    return json.loads(get_bytes(url, **kw))
