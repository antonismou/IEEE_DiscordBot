from __future__ import annotations

import io
import random

import pytest
from PIL import Image

from bot.quotes.backgrounds import (
    RANDOM_TERMS,
    fetch_giphy,
    generated_background,
    get_background,
    local_meme,
)

SEARCH_URL = "https://api.giphy.com/v1/gifs/search"
GIF_URL = "https://media.giphy.com/media/abc/giphy.gif"


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, content=b""):
        self.status_code = status_code
        self._json = json_data
        self.content = content

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


def _search_json(urls):
    return {"data": [{"images": {"downsized": {"url": u}}} for u in urls]}


class FakeGet:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, params=None, timeout=None, **kwargs):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _forbidden_get(*args, **kwargs):
    raise AssertionError("http_get must not be called")


def _is_image(b: bytes) -> bool:
    Image.open(io.BytesIO(b)).verify()
    return True


def test_random_terms_has_enough_entries():
    assert len(RANDOM_TERMS) >= 15
    assert all(isinstance(t, str) and t for t in RANDOM_TERMS)


def test_generated_background_is_animated_gif():
    data = generated_background(random.Random(1))
    img = Image.open(io.BytesIO(data))
    assert img.format == "GIF"
    assert getattr(img, "n_frames", 1) > 1


def test_giphy_success_returns_the_downloaded_gif():
    gif = generated_background(random.Random(2))
    get = FakeGet([FakeResponse(json_data=_search_json([GIF_URL])), FakeResponse(content=gif)])
    assert fetch_giphy("facepalm", "key", rng=random.Random(0), http_get=get) == gif
    assert len(get.calls) == 2
    assert get.calls[1]["url"] == GIF_URL


def test_giphy_sends_pg13_rating_and_the_term():
    gif = generated_background(random.Random(2))
    get = FakeGet([FakeResponse(json_data=_search_json([GIF_URL])), FakeResponse(content=gif)])
    fetch_giphy("mind blown", "secret", rng=random.Random(0), http_get=get)
    first = get.calls[0]
    assert first["url"] == SEARCH_URL
    assert first["params"]["rating"] == "pg-13"
    assert first["params"]["q"] == "mind blown"
    assert first["params"]["api_key"] == "secret"
    assert first["params"]["limit"] == 25
    assert all(c["timeout"] == 10 for c in get.calls)


def test_giphy_http_error_returns_none():
    get = FakeGet([FakeResponse(status_code=500)])
    assert fetch_giphy("x", "key", rng=random.Random(0), http_get=get) is None
    get = FakeGet([ConnectionError("boom")])
    assert fetch_giphy("x", "key", rng=random.Random(0), http_get=get) is None
    get = FakeGet([FakeResponse(json_data=_search_json([GIF_URL])), FakeResponse(status_code=404)])
    assert fetch_giphy("x", "key", rng=random.Random(0), http_get=get) is None


def test_giphy_junk_bytes_returns_none():
    get = FakeGet([FakeResponse(json_data=_search_json([GIF_URL])), FakeResponse(content=b"not an image")])
    assert fetch_giphy("x", "key", rng=random.Random(0), http_get=get) is None


def test_giphy_empty_results_returns_none():
    get = FakeGet([FakeResponse(json_data={"data": []})])
    assert fetch_giphy("x", "key", rng=random.Random(0), http_get=get) is None
    assert len(get.calls) == 1


def test_local_meme_picks_an_image_file(tmp_path):
    gif = generated_background(random.Random(3))
    (tmp_path / "a.gif").write_bytes(gif)
    (tmp_path / "notes.txt").write_text("ignore me")
    assert local_meme(tmp_path, random.Random(0)) == gif


def test_local_meme_missing_folder_returns_none(tmp_path):
    assert local_meme(tmp_path / "nope", random.Random(0)) is None


def test_no_key_skips_giphy_and_uses_local(tmp_path):
    gif = generated_background(random.Random(4))
    (tmp_path / "meme.gif").write_bytes(gif)
    out = get_background("facepalm", api_key="", memes_dir=tmp_path, rng=random.Random(0), http_get=_forbidden_get)
    assert out == gif


def test_whitespace_key_skips_giphy(tmp_path):
    gif = generated_background(random.Random(4))
    (tmp_path / "meme.gif").write_bytes(gif)
    out = get_background(None, api_key="   ", memes_dir=tmp_path, rng=random.Random(0), http_get=_forbidden_get)
    assert out == gif


def test_get_background_uses_random_term_when_none(tmp_path):
    get = FakeGet([FakeResponse(json_data={"data": []})])
    get_background(None, api_key="k", memes_dir=tmp_path, rng=random.Random(0), http_get=get)
    assert get.calls[0]["params"]["q"] in RANDOM_TERMS


def test_empty_folder_falls_back_to_generated(tmp_path):
    out = get_background("x", api_key="", memes_dir=tmp_path, rng=random.Random(0), http_get=_forbidden_get)
    assert _is_image(out)
    assert Image.open(io.BytesIO(out)).format == "GIF"


def test_get_background_never_raises_when_everything_fails(tmp_path, monkeypatch):
    import bot.quotes.backgrounds as bg

    def boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(bg, "fetch_giphy", boom)
    monkeypatch.setattr(bg, "local_meme", boom)
    out = get_background("x", api_key="k", memes_dir=tmp_path, rng=random.Random(0), http_get=boom)
    assert _is_image(out)


def test_get_background_survives_generated_failure_too(tmp_path, monkeypatch):
    import bot.quotes.backgrounds as bg

    def boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(bg, "fetch_giphy", boom)
    monkeypatch.setattr(bg, "local_meme", boom)
    monkeypatch.setattr(bg, "generated_background", boom)
    out = get_background("x", api_key="k", memes_dir=tmp_path, rng=random.Random(0), http_get=boom)
    assert _is_image(out)
