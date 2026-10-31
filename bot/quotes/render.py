"""Turn a background image plus a quote into an unhinged animated GIF.

Synchronous and CPU-bound: call it through ``asyncio.to_thread``. Every random
choice comes from the ``rng`` passed in, so a seeded ``random.Random`` gives the
same bytes every time.
"""

from __future__ import annotations

import io
import math
import random
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageSequence

FONT_PATH = Path(__file__).resolve().parents[2] / "assets" / "fonts" / "DejaVuSans-Bold.ttf"
MAX_GIF_BYTES = 8_000_000
MAX_FRAMES = 30
MAX_WIDTH = 480
MAX_HEIGHT = 480
MIN_WIDTH = 320  # tiny backgrounds are scaled up so the text stays readable
MIN_FRAMES = 8  # static backgrounds are padded so the result still animates
TYPEWRITER_MIN_FRAMES = 16
SHRINK_FLOOR_WIDTH = 120
DEFAULT_DURATION_MS = 90
TYPEWRITER_PROBABILITY = 0.3
ELLIPSIS = "…"

Frames = list[Image.Image]

STICKER_WORDS: tuple[str, ...] = ("LOL", "!?", "?!", "OMG", "BRUH", "XD", "WOW", "!!!", "NO", "RIP", "W", "L")
STICKER_COLORS: tuple[tuple[int, int, int], ...] = (
    (255, 230, 0),
    (255, 60, 60),
    (0, 255, 120),
    (0, 200, 255),
    (255, 0, 220),
    (255, 140, 0),
)


@lru_cache(maxsize=64)
def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_PATH), size)


def clip_text(text: str, limit: int = 280) -> str:
    """At most ``limit`` characters; longer text is cut and ends with an ellipsis."""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + ELLIPSIS if limit > 1 else ELLIPSIS


# --------------------------------------------------------------------------- effects


def _shake(frames: Frames, rng: random.Random) -> Frames:
    """Zoom in a little and crop at a random offset per frame: camera shake without borders."""
    w, h = frames[0].size
    zoom = rng.uniform(1.06, 1.14)
    zw, zh = max(w + 2, round(w * zoom)), max(h + 2, round(h * zoom))
    out = []
    for frame in frames:
        big = frame.resize((zw, zh), Image.Resampling.BILINEAR)
        x = rng.randint(0, zw - w)
        y = rng.randint(0, zh - h)
        out.append(big.crop((x, y, x + w, y + h)))
    return out


def _glitch(frames: Frames, rng: random.Random) -> Frames:
    """RGB channel split plus horizontally torn slices, different on every frame."""
    w, h = frames[0].size
    out = []
    for frame in frames:
        img = frame.copy()
        if rng.random() < 0.8:
            r, g, b = img.split()
            r = ImageChops.offset(r, rng.randint(-w // 30 - 2, w // 30 + 2), 0)
            b = ImageChops.offset(b, rng.randint(-w // 30 - 2, w // 30 + 2), rng.randint(-2, 2))
            img = Image.merge("RGB", (r, g, b))
        for _ in range(rng.randint(1, 6)):
            y0 = rng.randrange(h)
            y1 = min(h, y0 + rng.randint(2, max(3, h // 8)))
            band = img.crop((0, y0, w, y1))
            img.paste(ImageChops.offset(band, rng.randint(-w // 6, w // 6), 0), (0, y0))
        out.append(img)
    return out


def _deepfry(frames: Frames, rng: random.Random) -> Frames:
    """Oversaturate, overcontrast, oversharpen and JPEG-crush, with a flicker per frame."""
    color = rng.uniform(2.0, 4.0)
    contrast = rng.uniform(1.5, 2.4)
    sharp = rng.uniform(2.0, 5.0)
    quality = rng.randint(4, 12)
    tint = rng.choice(((255, 60, 0), (255, 0, 0), (255, 200, 0), None))
    out = []
    for frame in frames:
        img = ImageEnhance.Color(frame).enhance(color)
        img = ImageEnhance.Contrast(img).enhance(contrast * rng.uniform(0.9, 1.1))
        img = ImageEnhance.Brightness(img).enhance(rng.uniform(1.0, 1.2))
        img = ImageEnhance.Sharpness(img).enhance(sharp)
        if tint is not None:
            img = Image.blend(img, Image.new("RGB", img.size, tint), 0.18)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        buf.seek(0)
        img = Image.open(buf).convert("RGB")
        out.append(img.filter(ImageFilter.UnsharpMask(radius=2, percent=150, threshold=2)))
    return out


def _star(size: int, color: tuple[int, int, int]) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    c = size / 2
    points = []
    for i in range(10):
        radius = (size / 2 - 2) if i % 2 == 0 else (size / 5)
        angle = -math.pi / 2 + i * math.pi / 5
        points.append((c + radius * math.cos(angle), c + radius * math.sin(angle)))
    ImageDraw.Draw(img).polygon(points, fill=color + (255,), outline=(0, 0, 0, 255), width=max(1, size // 16))
    return img


def _word_sticker(word: str, size: int, color: tuple[int, int, int]) -> Image.Image:
    font = _font(size)
    stroke = max(2, size // 8)
    left, top, right, bottom = font.getbbox(word, stroke_width=stroke)
    img = Image.new("RGBA", (right - left + 4, bottom - top + 4), (0, 0, 0, 0))
    ImageDraw.Draw(img).text(
        (2 - left, 2 - top), word, font=font, fill=color + (255,), stroke_width=stroke, stroke_fill=(0, 0, 0, 255)
    )
    return img


def _stickers(frames: Frames, rng: random.Random) -> Frames:
    """Words and stars that pop in at random moments and wobble around."""
    w, h = frames[0].size
    n = len(frames)
    stickers = []
    for _ in range(rng.randint(2, 5)):
        color = rng.choice(STICKER_COLORS)
        size = rng.randint(max(12, w // 14), max(14, w // 7))
        if rng.random() < 0.35:
            art = _star(size + size // 2, color)
        else:
            art = _word_sticker(rng.choice(STICKER_WORDS), size, color)
        stickers.append(
            {
                "art": art,
                "x": rng.uniform(0.0, 0.85) * w,
                "y": rng.uniform(0.0, 0.85) * h,
                "angle": rng.uniform(-30, 30),
                "wobble": rng.uniform(5, 25),
                "phase": rng.uniform(0, 2 * math.pi),
                "start": rng.randrange(max(1, n // 2)),
            }
        )
    out = []
    for i, frame in enumerate(frames):
        img = frame.copy()
        for s in stickers:
            if i < s["start"]:
                continue
            t = 2 * math.pi * i / max(1, n)
            angle = s["angle"] + s["wobble"] * math.sin(t * 2 + s["phase"])
            art = s["art"].rotate(angle, expand=True, resample=Image.Resampling.BICUBIC)
            if i == s["start"]:  # pop in a bit oversized
                art = art.resize((max(1, art.width * 5 // 4), max(1, art.height * 5 // 4)))
            img.paste(art, (int(s["x"]), int(s["y"])), art)
        out.append(img)
    return out


EFFECTS: dict[str, Callable[[Frames, random.Random], Frames]] = {
    "shake": _shake,
    "glitch": _glitch,
    "deepfry": _deepfry,
    "stickers": _stickers,
}


def pick_effects(rng: random.Random) -> list[str]:
    """One to three distinct effects, plus ``typewriter`` 30% of the time."""
    names = rng.sample(sorted(EFFECTS), rng.randint(1, 3))
    if rng.random() < TYPEWRITER_PROBABILITY:
        names.append("typewriter")
    return names


# --------------------------------------------------------------------------- decoding


def _target_size(w: int, h: int) -> tuple[int, int]:
    w, h = max(1, w), max(1, h)
    scale = min(MAX_WIDTH / w, MAX_HEIGHT / h, max(1.0, MIN_WIDTH / w))
    return max(1, round(w * scale)), max(1, round(h * scale))


def _decode(background: bytes) -> tuple[Frames, int]:
    """Up to MAX_FRAMES RGB frames (evenly sampled), resized, and a per-frame duration in ms."""
    with Image.open(io.BytesIO(background)) as img:
        total = getattr(img, "n_frames", 1)
        step = max(1, math.ceil(total / MAX_FRAMES))
        size = _target_size(*img.size)
        frames: Frames = []
        durations: list[int] = []
        for index, frame in enumerate(ImageSequence.Iterator(img)):
            if index % step:
                continue
            durations.append(int(frame.info.get("duration") or DEFAULT_DURATION_MS))
            rgba = frame.convert("RGBA")
            base = Image.new("RGBA", rgba.size, (0, 0, 0, 255))
            base.alpha_composite(rgba)
            frames.append(base.convert("RGB").resize(size, Image.Resampling.LANCZOS))
            if len(frames) >= MAX_FRAMES:
                break
    durations.sort()
    duration = durations[len(durations) // 2] * step if durations else DEFAULT_DURATION_MS
    return frames, max(40, min(duration, 250))


def _pad_frames(frames: Frames, minimum: int) -> Frames:
    if len(frames) >= minimum:
        return frames
    return [frames[i % len(frames)].copy() for i in range(minimum)]


# --------------------------------------------------------------------------- text


def _wrap(text: str, font: ImageFont.FreeTypeFont, max_width: float) -> list[str]:
    """Greedy wrap by measured width; words wider than a line are hard-broken."""
    lines: list[str] = []
    for paragraph in text.splitlines() or [""]:
        line = ""
        for word in paragraph.split():
            candidate = f"{line} {word}" if line else word
            if font.getlength(candidate) <= max_width:
                line = candidate
                continue
            if line:
                lines.append(line)
                line = ""
            while font.getlength(word) > max_width and len(word) > 1:
                cut = 1
                while cut < len(word) and font.getlength(word[: cut + 1]) <= max_width:
                    cut += 1
                lines.append(word[:cut])
                word = word[cut:]
            line = word
        lines.append(line)
    while lines and not lines[-1]:
        lines.pop()
    return lines or [""]


def _stroke(size: int) -> int:
    return max(2, size // 12)


def _layout(text: str, w: int, h: int, max_height: float) -> tuple[ImageFont.FreeTypeFont, list[str], int]:
    """Largest font (scaled to width) whose wrapped block fits; returns font, lines, line height."""
    size = max(12, w // 9)
    while True:
        font = _font(size)
        lines = _wrap(text, font, w * 0.9 - 2 * _stroke(size))
        line_h = round(size * 1.18) + _stroke(size)
        if len(lines) * line_h <= max_height or size <= 9:
            return font, lines, line_h
        size = max(9, int(size * 0.9))


def _fit_line(text: str, w: int, start: int) -> tuple[ImageFont.FreeTypeFont, str]:
    """A single line that fits the width: shrink the font, then truncate with an ellipsis."""
    size = start
    while size > 8 and _font(size).getlength(text) + 2 * _stroke(size) > w * 0.94:
        size -= 1
    font = _font(size)
    while len(text) > 1 and font.getlength(text) + 2 * _stroke(size) > w * 0.94:
        text = text[:-2] + ELLIPSIS
    return font, text


def _draw_text(
    frames: Frames,
    *,
    text: str,
    credit: str,
    typewriter: bool,
    rng: random.Random,
) -> Frames:
    w, h = frames[0].size
    margin = max(4, h // 30)
    credit_font, credit = _fit_line(credit, w, max(10, w // 18))
    credit_stroke = _stroke(credit_font.size)
    cl, ct, cr, cb = credit_font.getbbox(credit, stroke_width=credit_stroke)
    credit_xy = ((w - (cr - cl)) / 2 - cl, h - margin - cb)
    credit_top = h - margin - (cb - ct)

    area = credit_top - 2 * margin
    font, lines, line_h = _layout(text, w, h, area)
    stroke = _stroke(font.size)
    block_h = len(lines) * line_h
    slack = max(0, area - block_h)
    top = margin + (0 if rng.random() < 0.5 else slack // 2)
    fill = rng.choice(((255, 255, 255), (255, 255, 255), (255, 255, 255), (255, 240, 80)))

    total_chars = sum(len(line) for line in lines)
    n = len(frames)
    reveal_by = max(1, round(n * 0.6))
    out = []
    for i, frame in enumerate(frames):
        img = frame.copy()
        draw = ImageDraw.Draw(img)
        shown = total_chars if not typewriter else math.ceil(total_chars * min(1.0, (i + 1) / reveal_by))
        y = top
        for line in lines:
            visible = line[: max(0, shown)]
            shown -= len(line)
            if visible:
                # Position by the full line so the text does not slide while it is typed out.
                x = (w - font.getlength(line)) / 2
                draw.text((x, y), visible, font=font, fill=fill, stroke_width=stroke, stroke_fill=(0, 0, 0))
            y += line_h
        draw.text(credit_xy, credit, font=credit_font, fill=(255, 255, 255), stroke_width=credit_stroke, stroke_fill=(0, 0, 0))
        out.append(img)
    return out


# --------------------------------------------------------------------------- encoding


def _encode(frames: Frames, duration: int) -> bytes:
    # FASTOCTREE is ~100x faster than MEDIANCUT on busy frames and looks fine for memes.
    palette = [f.quantize(colors=256, method=Image.Quantize.FASTOCTREE) for f in frames]
    out = io.BytesIO()
    palette[0].save(out, format="GIF", save_all=True, append_images=palette[1:], duration=duration, loop=0)
    return out.getvalue()


def render_quote_gif(
    background: bytes,
    *,
    text: str,
    author: str,
    year: int,
    rng: random.Random,
    max_bytes: int = MAX_GIF_BYTES,
) -> bytes:
    """Blocking. Renders the quote over the background with random effects as a looping GIF."""
    frames, duration = _decode(background)
    effects = pick_effects(rng)
    typewriter = "typewriter" in effects
    frames = _pad_frames(frames, TYPEWRITER_MIN_FRAMES if typewriter else MIN_FRAMES)
    for name in effects:
        if name in EFFECTS:
            frames = EFFECTS[name](frames, rng)
    frames = _draw_text(frames, text=text, credit=f"— {author}, {year}", typewriter=typewriter, rng=rng)

    data = _encode(frames, duration)
    while len(data) > max_bytes and frames[0].width > SHRINK_FLOOR_WIDTH:
        w, h = frames[0].size
        new_w = max(SHRINK_FLOOR_WIDTH, int(w * 0.75))
        new_h = max(1, round(h * new_w / w))
        if len(frames) > 2:
            frames = frames[::2]
            duration = min(duration * 2, 500)
        frames = [f.resize((new_w, new_h), Image.Resampling.LANCZOS) for f in frames]
        data = _encode(frames, duration)
    return data
