"""Final output with ffmpeg: cut, encode, tag, embed cover art."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from . import __version__, net, tools
from .model import CancelToken, Episode, ReelcastError, Reporter

FORMATS = {
    # extension: (ffmpeg muxer, codec name per ffprobe, encoder arguments, cover supported)
    "mp3": ("mp3", "mp3", lambda q: ["-c:a", "libmp3lame", "-q:a", str(q)], True),
    "m4a": ("ipod", "aac", lambda q: ["-c:a", "aac", "-b:a", "160k"], True),
    "opus": ("ogg", "opus", lambda q: ["-c:a", "libopus", "-b:a", "96k"], False),
    "flac": ("flac", "flac", lambda q: ["-c:a", "flac"], True),
}


def probe(path: Path) -> tuple[float, str]:
    """(duration in seconds, codec of the first audio stream)."""
    p = subprocess.run(
        [
            tools.ffprobe(), "-v", "error", "-select_streams", "a:0",
            "-show_entries", "format=duration:stream=codec_name", "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
    )
    if p.returncode != 0:
        raise ReelcastError(f"Can't read file: {path.name}")
    d = json.loads(p.stdout or "{}")
    codec = (d.get("streams") or [{}])[0].get("codec_name", "")
    return float(d.get("format", {}).get("duration") or 0), codec


def fetch_cover(url: str) -> bytes | None:
    if not url:
        return None
    urls = [url]
    if "/image/" in url:  # the same image ID lives on several Spotify CDNs
        image_id = url.rsplit("/image/", 1)[1]
        urls += [f"https://i.scdn.co/image/{image_id}"]
    for u in urls:
        try:
            data = net.get_bytes(u, retries=1)
        except Exception:
            continue
        if data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n":
            return data
    return None


def episode_tags(ep: Episode, source_note: str = "") -> dict[str, str]:
    return {
        "title": ep.title,
        "artist": ep.show,
        "album_artist": ep.show,
        "album": ep.show,
        "date": ep.release,
        "genre": "Podcast",
        "comment": f"{ep.url} – {source_note} – Reelcast {__version__}".strip(" –"),
    }


def finalize(
    src: Path,
    dest: Path,
    tags: dict[str, str],
    fmt: str,
    quality: int,
    cover: bytes | None,
    reporter: Reporter,
    cancel: CancelToken,
    segments: list[tuple[int, int]] | None = None,
    rate: int = 48000,
    copy: bool = False,
) -> None:
    muxer, _codec, enc_args, cover_ok = FORMATS[fmt]
    cmd = [tools.ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(src)]
    cover_file = None
    if cover and cover_ok:
        fd, name = tempfile.mkstemp(prefix="reelcast-cover-", suffix=".jpg")
        with os.fdopen(fd, "wb") as f:
            f.write(cover)
        cover_file = Path(name)
        cmd += ["-i", str(cover_file)]

    total = sum(b - a for a, b in segments) / rate if segments else probe(src)[0]
    if segments:
        parts = [
            f"[0:a]atrim=start_sample={a}:end_sample={b},asetpts=PTS-STARTPTS[s{i}]"
            for i, (a, b) in enumerate(segments)
        ]
        joined = "".join(f"[s{i}]" for i in range(len(segments)))
        parts.append(f"{joined}concat=n={len(segments)}:v=0:a=1[out]")
        cmd += ["-filter_complex", ";".join(parts), "-map", "[out]"]
        copy = False
    else:
        cmd += ["-map", "0:a:0"]
    cmd += ["-c:a", "copy"] if copy else enc_args(quality)
    if cover_file:
        cmd += ["-map", "1:v", "-c:v", "copy", "-disposition:v:0", "attached_pic"]
        if fmt == "mp3":
            cmd += ["-metadata:s:v", "title=Album cover", "-metadata:s:v", "comment=Cover (front)"]
    for k, v in tags.items():
        if v:
            cmd += ["-metadata", f"{k}={v}"]
    if fmt == "mp3":
        cmd += ["-id3v2_version", "3", "-write_id3v1", "0"]
    if fmt == "m4a":
        cmd += ["-movflags", "+faststart"]
    part = dest.with_name(dest.name + ".part")
    cmd += ["-progress", "pipe:1", "-f", muxer, str(part)]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        for line in proc.stdout:
            if cancel.cancelled:
                proc.kill()
                break
            if line.startswith("out_time_us=") and total:
                try:
                    reporter.progress(min(int(line.split("=", 1)[1]) / 1e6, total), total)
                except ValueError:
                    pass
        proc.wait()
        cancel.check()
        if proc.returncode != 0:
            raise ReelcastError(f"ffmpeg: {proc.stderr.read().strip()[-400:]}")
        os.replace(part, dest)
        reporter.progress(total, total)
    finally:
        if proc.poll() is None:
            proc.kill()
        part.unlink(missing_ok=True)
        if cover_file:
            cover_file.unlink(missing_ok=True)
