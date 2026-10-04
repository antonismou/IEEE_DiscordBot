from __future__ import annotations

import re
import unicodedata

_LOCAL = re.compile(r"[a-z0-9._%+\-]+")
_DOMAIN = re.compile(r"(?:[a-z0-9](?:[a-z0-9\-]*[a-z0-9])?\.)*tuc\.gr")


def normalize_email(email: str) -> str:
    return email.strip().lower()


def is_tuc_email(email: str) -> bool:
    """True for local@tuc.gr and local@<labels>.tuc.gr, ASCII only."""
    raw = email.strip()
    if not raw.isascii():  # checked before lower(): e.g. Kelvin sign lowercases to ASCII 'k'
        return False
    candidate = raw.lower()
    if candidate.count("@") != 1:
        return False
    local, domain = candidate.split("@")
    return bool(_LOCAL.fullmatch(local)) and bool(_DOMAIN.fullmatch(domain))


def clean_text(raw: str, *, min_len: int, max_len: int, label: str) -> str:
    text = " ".join(raw.split())
    if any(unicodedata.category(ch).startswith("C") for ch in text):
        raise ValueError(f"{label} contains invalid characters.")
    if len(text) < min_len:
        raise ValueError(f"{label} must be at least {min_len} characters.")
    if len(text) > max_len:
        raise ValueError(f"{label} must be at most {max_len} characters.")
    return text
