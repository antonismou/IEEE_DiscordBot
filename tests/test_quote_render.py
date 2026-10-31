from __future__ import annotations

import io
import random

import pytest
from PIL import Image

from bot.quotes.backgrounds import generated_background
from bot.quotes.render import (
    EFFECTS,
    FONT_PATH,
    MAX_FRAMES,
    MAX_WIDTH,
    clip_text,
    pick_effects,
    render_quote_gif,
)


def _open(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    img.load()
    return img


def _frames(n: int = 4, size: tuple[int, int] = (160, 120)) -> list[Image.Image]:
    rng = random.Random(1)
    return [
        Image.new("RGB", size, tuple(rng.randrange(256) for _ in range(3))) for _ in range(n)
    ]


def test_font_path_exists_regardless_of_cwd():
    assert FONT_PATH.is_absolute()
    assert FONT_PATH.is_file()


def test_clip_text_trims_with_ellipsis():
    long = "x" * 281
    clipped = clip_text(long)
    assert len(clipped) == 280
    assert clipped.endswith("…")
    assert clip_text("short quote") == "short quote"
    assert clip_text("y" * 280) == "y" * 280


def test_pick_effects_returns_known_names_and_is_seeded():
    allowed = set(EFFECTS) | {"typewriter"}
    for seed in range(50):
        picked = pick_effects(random.Random(seed))
        assert set(picked) <= allowed
        assert len(picked) == len(set(picked))
        base = [p for p in picked if p != "typewriter"]
        assert 1 <= len(base) <= 3
        assert picked == pick_effects(random.Random(seed))
    assert set(EFFECTS) == {"shake", "glitch", "deepfry", "stickers"}


@pytest.mark.parametrize("name", sorted(EFFECTS))
def test_each_effect_keeps_frame_count_and_size(name):
    frames = _frames()
    out = EFFECTS[name](frames, random.Random(3))
    assert len(out) == len(frames)
    assert all(f.size == frames[0].size for f in out)


def test_render_returns_an_animated_gif():
    bg = generated_background(random.Random(5))
    data = render_quote_gif(bg, text="I'll fix it after lunch, probably.", author="Nikos", year=2026, rng=random.Random(7))
    img = _open(data)
    assert img.format == "GIF"
    assert img.is_animated
    assert img.width <= MAX_WIDTH
    assert img.n_frames <= MAX_FRAMES


def test_render_is_deterministic_for_a_seed():
    bg = generated_background(random.Random(5))
    a = render_quote_gif(bg, text="same", author="A", year=2025, rng=random.Random(11))
    b = render_quote_gif(bg, text="same", author="A", year=2025, rng=random.Random(11))
    assert a == b


def test_greek_text_renders():
    bg = generated_background(random.Random(2))
    data = render_quote_gif(bg, text="Καλημέρα σε όλους, τι κάνετε;", author="Αντώνης", year=2026, rng=random.Random(4))
    assert _open(data).format == "GIF"


def test_long_unbroken_word_renders():
    bg = generated_background(random.Random(2))
    data = render_quote_gif(bg, text="a" * 200, author="B", year=2026, rng=random.Random(9))
    img = _open(data)
    assert img.format == "GIF"
    assert img.is_animated


def _noisy_gif(frames: int, size: tuple[int, int]) -> bytes:
    rng = random.Random(0)
    w, h = size
    palette = list(rng.randbytes(768))
    images = []
    for _ in range(frames):
        # Random palette indices give incompressible noise cheaply: no per-pixel loops, no quantizing.
        img = Image.frombytes("P", (w // 4, h // 4), rng.randbytes((w // 4) * (h // 4)))
        img.putpalette(palette)
        images.append(img.resize(size, Image.Resampling.NEAREST))
    out = io.BytesIO()
    images[0].save(out, format="GIF", save_all=True, append_images=images[1:], duration=50, loop=0)
    return out.getvalue()


def test_huge_background_fits_the_byte_cap():
    bg = _noisy_gif(60, (1200, 900))
    data = render_quote_gif(bg, text="too big", author="C", year=2026, rng=random.Random(1), max_bytes=300_000)
    assert len(data) <= 300_000
    assert _open(data).format == "GIF"


def test_static_png_background_renders():
    buf = io.BytesIO()
    Image.new("RGB", (640, 480), (30, 90, 200)).save(buf, format="PNG")
    data = render_quote_gif(buf.getvalue(), text="static bg", author="D", year=2026, rng=random.Random(2))
    img = _open(data)
    assert img.format == "GIF"
    assert img.is_animated
    assert img.n_frames >= 2
