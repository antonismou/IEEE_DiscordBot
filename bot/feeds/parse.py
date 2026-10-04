from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser

import feedparser
import requests

USER_AGENT = "IEEE-TUC-DiscordBot/1.0 (student branch RSS reader)"
TIMEOUT_SECONDS = 20
MAX_FEED_BYTES = 5_000_000
SUMMARY_LIMIT = 300


class FeedError(Exception):
    pass


@dataclass(frozen=True)
class FeedItem:
    item_id: str
    title: str
    link: str | None
    summary: str
    image_url: str | None
    published: datetime | None


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.image: str | None = None
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "img" and self.image is None:
            src = dict(attrs).get("src") or ""
            if src.startswith(("http://", "https://")):
                self.image = src
        elif tag in ("br", "p", "div", "li"):
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def _text_and_image(html: str) -> tuple[str, str | None]:
    extractor = _TextExtractor()
    extractor.feed(html)
    extractor.close()
    return " ".join("".join(extractor.parts).split()), extractor.image


def _http_url(value) -> str | None:
    return value if isinstance(value, str) and value.startswith(("http://", "https://")) else None


def parse_feed(content: bytes) -> list[FeedItem]:
    parsed = feedparser.parse(content)
    if parsed.bozo and not parsed.entries:
        raise FeedError("Response is not a valid RSS/Atom feed")
    items: list[FeedItem] = []
    for entry in parsed.entries:
        link = _http_url(entry.get("link"))
        item_id = (entry.get("id") or link or "").strip()
        title = " ".join(str(entry.get("title", "")).split())
        if not item_id or not title:
            continue
        raw = entry.get("summary", "") or ""
        if raw.strip().lower() == "null":  # IEEE Xplore puts the literal text "null" here
            raw = ""
        text, image = _text_and_image(raw)
        if image is None:
            for key in ("media_content", "media_thumbnail"):
                for media in entry.get(key, []) or []:
                    image = _http_url(media.get("url"))
                    if image:
                        break
                if image:
                    break
        stamp = entry.get("published_parsed")
        published = datetime(*stamp[:6], tzinfo=timezone.utc) if stamp else None
        items.append(FeedItem(item_id, title, link, truncate(text, SUMMARY_LIMIT), image, published))
    return items


def fetch_feed(url: str) -> bytes:
    try:
        with requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_SECONDS, stream=True) as response:
            if response.status_code != 200:
                raise FeedError(f"HTTP {response.status_code} from {url}")
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > MAX_FEED_BYTES:
                    raise FeedError(f"Feed is too large: {url}")
                chunks.append(chunk)
            return b"".join(chunks)
    except requests.RequestException as exc:
        raise FeedError(f"{type(exc).__name__}: {exc}") from exc
