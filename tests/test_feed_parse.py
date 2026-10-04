import pytest
import requests

from bot.feeds import parse
from bot.feeds.parse import FeedError, fetch_feed, parse_feed, truncate

XPLORE = b"""<?xml version="1.0" encoding="UTF-8" ?>
<rss version="2.0"><channel><title><![CDATA[ IEEE Transactions on Robotics - new TOC ]]></title>
<item>
  <title><![CDATA[Guest Editorial: Event-Based Vision for Robotics]]></title>
  <link><![CDATA[http://ieeexplore.ieee.org/document/11688145]]></link>
  <description><![CDATA[null]]></description>
  <pubDate><![CDATA[FRI, 11 SEP 2026 01:06:42 -0400]]></pubDate>
  <guid><![CDATA[http://ieeexplore.ieee.org/document/11688145]]>
  </guid>
</item></channel></rss>"""

SPECTRUM = b"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom"><channel><title>IEEE Spectrum</title>
<item>
  <title>Robots Learn to Walk</title>
  <link>https://spectrum.ieee.org/robots-walk</link>
  <guid isPermaLink="false">spectrum-1</guid>
  <description><![CDATA[<img src="https://spectrum.ieee.org/media/robot.jpg"/><br/><p>Legged <b>robots</b> are
  getting &amp; better.</p><script>alert(1)</script>]]></description>
  <pubDate>Mon, 05 Oct 2026 10:00:00 GMT</pubDate>
</item></channel></rss>"""


def test_xplore_item_ignores_null_description():
    (item,) = parse_feed(XPLORE)
    assert item.item_id == "http://ieeexplore.ieee.org/document/11688145"
    assert item.title == "Guest Editorial: Event-Based Vision for Robotics"
    assert item.link == "http://ieeexplore.ieee.org/document/11688145"
    assert item.summary == "" and item.image_url is None
    assert item.published is not None and item.published.year == 2026


def test_spectrum_item_strips_html_and_extracts_image():
    (item,) = parse_feed(SPECTRUM)
    assert item.item_id == "spectrum-1"
    assert item.summary == "Legged robots are getting & better."
    assert "alert" not in item.summary
    assert item.image_url == "https://spectrum.ieee.org/media/robot.jpg"


def test_summary_is_truncated_to_300_characters():
    long = SPECTRUM.replace(b"Legged", b"x" * 500)
    (item,) = parse_feed(long)
    assert len(item.summary) <= 300 and item.summary.endswith("…")


def test_html_error_page_is_an_error_not_an_empty_feed():
    with pytest.raises(FeedError):
        parse_feed(b"<html><head><title>I'm a teapot</title></head><body><h1>418</title></body></html>")


def test_plain_text_is_an_error():
    with pytest.raises(FeedError):
        parse_feed(b"I'm a Teapot")


def test_valid_empty_feed_is_an_empty_list():
    assert parse_feed(b'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title></channel></rss>') == []


def test_non_http_links_are_dropped_and_id_falls_back_to_link():
    xml = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
    <item><title>Bad link</title><link>javascript:alert(1)</link><guid>g1</guid></item>
    <item><title>No guid</title><link>https://example.com/a</link></item>
    <item><title>No id at all</title></item>
    </channel></rss>"""
    items = parse_feed(xml)
    assert [i.item_id for i in items] == ["g1", "https://example.com/a"]
    assert items[0].link is None and items[1].link == "https://example.com/a"


def test_truncate():
    assert truncate("abc", 5) == "abc"
    assert truncate("abcdef", 4) == "abc…"


class FakeResponse:
    def __init__(self, status=200, chunks=(b"data",)):
        self.status_code, self._chunks = status, chunks

    def iter_content(self, size):
        return iter(self._chunks)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_fetch_returns_body_and_sends_user_agent(monkeypatch):
    seen = {}

    def fake_get(url, headers, timeout, stream):
        seen.update(url=url, headers=headers, timeout=timeout)
        return FakeResponse(chunks=(b"ab", b"cd"))

    monkeypatch.setattr(parse.requests, "get", fake_get)
    assert fetch_feed("https://x/feed") == b"abcd"
    assert seen["headers"]["User-Agent"] == parse.USER_AGENT and seen["timeout"] == 20


def test_fetch_reports_http_status(monkeypatch):
    monkeypatch.setattr(parse.requests, "get", lambda *a, **k: FakeResponse(status=418))
    with pytest.raises(FeedError, match="418"):
        fetch_feed("https://x/feed")


def test_fetch_wraps_network_errors(monkeypatch):
    def boom(*a, **k):
        raise requests.Timeout("slow")

    monkeypatch.setattr(parse.requests, "get", boom)
    with pytest.raises(FeedError):
        fetch_feed("https://x/feed")


def test_fetch_rejects_oversized_feeds(monkeypatch):
    monkeypatch.setattr(parse, "MAX_FEED_BYTES", 5)
    monkeypatch.setattr(parse.requests, "get", lambda *a, **k: FakeResponse(chunks=(b"abc", b"def")))
    with pytest.raises(FeedError, match="large"):
        fetch_feed("https://x/feed")
