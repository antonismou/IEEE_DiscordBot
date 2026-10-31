"""Where /quoteit meme backgrounds come from: Giphy, then a local folder, then a generated gradient."""

from __future__ import annotations

import io
import logging
import random
from pathlib import Path

import requests
from PIL import Image

log = logging.getLogger(__name__)

GIPHY_SEARCH_URL = "https://api.giphy.com/v1/gifs/search"
GIPHY_RATING = "pg-13"
GIPHY_LIMIT = 25
HTTP_TIMEOUT = 10

IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})

RANDOM_TERMS: tuple[str, ...] = (
    "facepalm",
    "mind blown",
    "awkward",
    "chaos",
    "shocked",
    "eye roll",
    "slow clap",
    "this is fine",
    "confused",
    "mic drop",
    "deal with it",
    "plot twist",
    "cringe",
    "wow",
    "popcorn",
    "side eye",
    "dramatic",
    "nope",
)

# 1x1 GIF used only if generating the gradient itself somehow fails.
_FALLBACK_GIF = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00"
    b",\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
)


def _is_image(data: bytes) -> bool:
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.verify()
        return True
    except Exception:
        return False


def fetch_giphy(term: str, api_key: str, *, rng: random.Random, http_get=requests.get) -> bytes | None:
    """One search plus one download; None on any failure."""
    try:
        resp = http_get(
            GIPHY_SEARCH_URL,
            params={"api_key": api_key, "q": term, "limit": GIPHY_LIMIT, "rating": GIPHY_RATING},
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code != 200:
            log.warning("giphy search returned HTTP %s", resp.status_code)
            return None
        urls = [
            item["images"]["downsized"]["url"]
            for item in resp.json().get("data", [])
            if item.get("images", {}).get("downsized", {}).get("url")
        ]
        if not urls:
            log.warning("giphy search for %r returned no results", term)
            return None
        gif = http_get(rng.choice(urls), timeout=HTTP_TIMEOUT)
        if gif.status_code != 200:
            log.warning("giphy download returned HTTP %s", gif.status_code)
            return None
        data = gif.content
        if not _is_image(data):
            log.warning("giphy download was not a readable image")
            return None
        return data
    except Exception:
        log.warning("giphy fetch failed", exc_info=True)
        return None


def local_meme(memes_dir: Path, rng: random.Random) -> bytes | None:
    """A random image from the memes folder; None if there is none or it can't be read."""
    try:
        files = sorted(
            p for p in Path(memes_dir).iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS
        )
        if not files:
            return None
        data = rng.choice(files).read_bytes()
        if not _is_image(data):
            log.warning("local meme in %s was not a readable image", memes_dir)
            return None
        return data
    except FileNotFoundError:
        return None
    except Exception:
        log.warning("reading local memes from %s failed", memes_dir, exc_info=True)
        return None


def generated_background(rng: random.Random) -> bytes:
    """A small animated GIF of a gradient that drifts between two random colours."""
    width, height, frames = 320, 240, 8
    a = tuple(rng.randrange(256) for _ in range(3))
    b = tuple(rng.randrange(256) for _ in range(3))
    images = []
    for f in range(frames):
        shift = f / frames
        column = Image.new("RGB", (1, height))
        px = column.load()
        for y in range(height):
            t = (y / max(height - 1, 1) + shift) % 1.0
            t = 1 - abs(2 * t - 1)  # triangle wave so the loop is seamless
            px[0, y] = tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))
        images.append(column.resize((width, height)).convert("P", palette=Image.Palette.ADAPTIVE))
    out = io.BytesIO()
    images[0].save(out, format="GIF", save_all=True, append_images=images[1:], duration=120, loop=0)
    return out.getvalue()


def get_background(
    term: str | None,
    *,
    api_key: str,
    memes_dir: Path,
    rng: random.Random,
    http_get=requests.get,
) -> bytes:
    """Blocking; never raises. Giphy (if keyed), else local folder, else a generated gradient."""
    key = (api_key or "").strip()
    if key:
        try:
            search = term if term else rng.choice(RANDOM_TERMS)
            data = fetch_giphy(search, key, rng=rng, http_get=http_get)
            if data:
                return data
        except Exception:
            log.warning("giphy background failed", exc_info=True)
    try:
        data = local_meme(memes_dir, rng)
        if data:
            return data
    except Exception:
        log.warning("local background failed", exc_info=True)
    try:
        return generated_background(rng)
    except Exception:
        log.warning("generated background failed", exc_info=True)
        return _FALLBACK_GIF
