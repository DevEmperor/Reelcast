"""Kleine HTTP-Helfer auf Basis der Standardbibliothek."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import __version__
from .model import CancelToken, ReelcastError, Reporter

UA = f"Mozilla/5.0 (X11; Linux x86_64) Reelcast/{__version__}"
TIMEOUT = 20


def _request(url: str, headers: dict | None = None) -> urllib.request.Request:
    h = {"User-Agent": UA, "Accept-Language": "de,en;q=0.8"}
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
    raise ReelcastError(f"Netzwerkfehler bei {urllib.parse.urlsplit(url).netloc}: {last}")


def get_text(url: str, **kw) -> str:
    return get_bytes(url, **kw).decode("utf-8", "replace")


def get_json(url: str, **kw):
    return json.loads(get_bytes(url, **kw))


def download(url: str, dest: Path, reporter: Reporter, cancel: CancelToken) -> str:
    """Lädt `url` nach `dest` (über eine .part-Datei). Gibt den Content-Type zurück."""
    part = dest.with_name(dest.name + ".part")
    try:
        with urllib.request.urlopen(_request(url), timeout=TIMEOUT) as r:
            total = int(r.headers.get("Content-Length") or 0) or None
            ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip()
            done = 0
            last_report = 0.0
            with open(part, "wb") as f:
                while True:
                    cancel.check()
                    chunk = r.read(256 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    now = time.monotonic()
                    if now - last_report > 0.1:
                        reporter.progress(done, total, "B")
                        last_report = now
            reporter.progress(done, total or done, "B")
        if total and done < total:
            raise ReelcastError(f"Download unvollständig ({done} von {total} Bytes)")
        os.replace(part, dest)
        return ctype
    except BaseException:
        part.unlink(missing_ok=True)
        raise
