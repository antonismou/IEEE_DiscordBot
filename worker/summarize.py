"""Summary stage of the meeting job: transcript.txt in, a Greek summary.md out.

The transcript (lines `[hh:mm:ss] Name: text`) is split into pieces of about `chunk_minutes` by timestamp. Each
piece gets an extraction pass (topics, decisions, tasks, open questions), then a merge pass writes the summary. A
transcript that fits in one piece is summarized in a single call. Every model call goes through `generate(prompt)`,
which defaults to a local Ollama server, so tests inject a fake. Meeting status is left to the caller.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import requests

DEFAULT_MODEL = "gemma3:12b"
DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_CHUNK_MINUTES = 12
NUM_CTX = 8192
TEMPERATURE = 0.2
REQUEST_TIMEOUT = 3600  # seconds; one piece takes several minutes on the server's CPU

HEADINGS = ("Θέματα", "Αποφάσεις", "Εργασίες", "Ανοιχτά θέματα")
EMPTY_SECTION = "Δεν υπάρχουν."

Generate = Callable[[str], str]


class OllamaError(Exception):
    """One failed call to Ollama: an HTTP error or no connection."""


class SummaryError(Exception):
    """The summary stage could not finish."""


# --- transcript ---------------------------------------------------------------------------------------------------

TIMESTAMP = re.compile(r"^\[(\d+):(\d{2}):(\d{2})\]")


@dataclass(frozen=True)
class Line:
    seconds: int
    text: str


def parse_transcript(text: str) -> list[Line]:
    """Timestamped lines; a line without a timestamp belongs to the line before it."""
    lines: list[Line] = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        match = TIMESTAMP.match(raw)
        if match:
            h, m, s = (int(g) for g in match.groups())
            lines.append(Line(h * 3600 + m * 60 + s, raw))
        elif lines:
            lines[-1] = Line(lines[-1].seconds, f"{lines[-1].text}\n{raw}")
        else:
            lines.append(Line(0, raw))
    return lines


def chunk_lines(lines: list[Line], chunk_minutes: int = DEFAULT_CHUNK_MINUTES) -> list[list[Line]]:
    """Groups whole lines into pieces that span less than `chunk_minutes` from their first line."""
    window = chunk_minutes * 60
    chunks: list[list[Line]] = []
    for line in lines:
        if chunks and line.seconds - chunks[-1][0].seconds < window:
            chunks[-1].append(line)
        else:
            chunks.append([line])
    return chunks


# --- prompts ------------------------------------------------------------------------------------------------------

RULES = (
    "Κανόνες:\n"
    "- Γράψε στα ελληνικά. Μην προσθέτεις τίποτα που δεν υπάρχει στο κείμενο: κανένα συμπέρασμα, όνομα, "
    "ημερομηνία, αριθμό ή εργασία που δεν ειπώθηκε.\n"
    "- Στις Εργασίες γράψε ΜΟΝΟ εργασίες που ανατέθηκαν ρητά σε κάποιον ή που κάποιος ανέλαβε ρητά, "
    "με υπεύθυνο και προθεσμία. Αν δεν ειπώθηκε προθεσμία, γράψε «χωρίς προθεσμία».\n"
    "- Αν μια απόφαση, ημερομηνία ή αριθμός διορθώθηκε αργότερα, γράψε μόνο την τελική εκδοχή.\n"
    "- Θέματα που δεν αποφασίστηκαν ή αναβλήθηκαν γράφονται στα Ανοιχτά θέματα, όχι στις Αποφάσεις.\n"
    "- Κράτησε ακριβώς τους αριθμούς, τα ποσά, τις ημερομηνίες και τα ονόματα του κειμένου.\n"
    f"- Αν μια ενότητα δεν έχει τίποτα, γράψε «{EMPTY_SECTION}»\n"
)

FORMAT = (
    "Γράψε την απάντηση ακριβώς με αυτές τις τέσσερις επικεφαλίδες, με αυτή τη σειρά, και κουκκίδες «- » "
    "από κάτω:\n\n" + "\n".join(f"## {h}\n- ...\n" for h in HEADINGS)
)


def extract_prompt(chunk_text: str, part: int, total: int) -> str:
    return (
        f"Παρακάτω είναι το μέρος {part} από {total} της απομαγνητοφώνησης μιας συνάντησης. Κάθε γραμμή είναι "
        "«[ώρα] Όνομα: λόγια». Κράτησε σημειώσεις για αυτό το μέρος με τις ενότητες Θέματα, Αποφάσεις, "
        "Εργασίες (με υπεύθυνο και προθεσμία) και Ανοιχτά θέματα.\n\n"
        f"{RULES}\nΑπομαγνητοφώνηση:\n{chunk_text}\n"
    )


def merge_prompt(notes: list[str]) -> str:
    parts = "\n\n".join(f"### Μέρος {i} από {len(notes)}\n{n.strip()}" for i, n in enumerate(notes, 1))
    return (
        "Παρακάτω είναι σημειώσεις από διαδοχικά μέρη της ίδιας συνάντησης, με χρονολογική σειρά. Ένωσέ τες σε "
        "μία σύνοψη χωρίς επαναλήψεις. Αν κάτι ήταν ανοιχτό σε ένα μέρος και αποφασίστηκε σε επόμενο, γράψε το "
        "στις Αποφάσεις. Αν κάτι άλλαξε σε επόμενο μέρος, ισχύει το επόμενο.\n\n"
        f"{RULES}\n{FORMAT}\nΣημειώσεις:\n{parts}\n"
    )


def final_prompt(transcript: str) -> str:
    return (
        "Παρακάτω είναι η απομαγνητοφώνηση μιας συνάντησης. Κάθε γραμμή είναι «[ώρα] Όνομα: λόγια». "
        "Γράψε τη σύνοψή της.\n\n"
        f"{RULES}\n{FORMAT}\nΑπομαγνητοφώνηση:\n{transcript}\n"
    )


# --- headings -----------------------------------------------------------------------------------------------------

def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c)).replace("ς", "σ")


HEADING_KEYS = {_normalize(h): h for h in HEADINGS}


def _heading_of(raw: str) -> str | None:
    text = re.sub(r"\(.*?\)", "", raw)
    text = re.sub(r"^[\s#*_]*(\d+[.)]\s*)?", "", text)
    text = text.strip(" \t*_:#")
    return HEADING_KEYS.get(_normalize(text).strip())


def ensure_headings(text: str) -> str:
    """Rebuilds the model's answer under the four fixed headings, in order, with a placeholder for empty ones."""
    sections: dict[str, list[str]] = {h: [] for h in HEADINGS}
    preamble: list[str] = []
    current: str | None = None
    for raw in text.splitlines():
        heading = _heading_of(raw) if raw.strip() else None
        if heading:
            current = heading
        else:
            (sections[current] if current else preamble).append(raw)
    if current is None:  # no heading at all: keep the text rather than lose it
        sections[HEADINGS[0]] = preamble
    blocks = []
    for heading in HEADINGS:
        body = "\n".join(sections[heading]).strip()
        blocks.append(f"## {heading}\n{body or EMPTY_SECTION}")
    return "\n\n".join(blocks)


# --- Ollama -------------------------------------------------------------------------------------------------------

def make_ollama_generate(model: str = DEFAULT_MODEL, base_url: str = DEFAULT_OLLAMA_URL,
                         session=None, timeout: float = REQUEST_TIMEOUT) -> Generate:
    """A `generate(prompt)` that calls Ollama once; the model unloads after each call (keep_alive 0)."""
    http = session or requests.Session()
    url = f"{base_url.rstrip('/')}/api/generate"

    def generate(prompt: str) -> str:
        body = {
            "model": model, "prompt": prompt, "stream": False, "keep_alive": 0,
            "options": {"temperature": TEMPERATURE, "num_ctx": NUM_CTX},
        }
        try:
            r = http.post(url, json=body, timeout=timeout)
        except requests.RequestException as exc:
            raise OllamaError(f"cannot reach Ollama at {base_url}: {exc}") from exc
        if not r.ok:
            raise OllamaError(f"Ollama returned HTTP {r.status_code}: {r.text[:300]}")
        return r.json().get("response", "").strip()

    return generate


def _call(generate: Generate, prompt: str, what: str) -> str:
    try:
        return generate(prompt)
    except OllamaError:
        pass  # one unexplained HTTP 500 happened in a benchmark and did not repeat
    try:
        return generate(prompt)
    except OllamaError as exc:
        raise SummaryError(f"summary model failed twice on {what}: {exc}") from exc


# --- the stage ----------------------------------------------------------------------------------------------------

def summarize_meeting(meeting_dir: Path | str, generate: Generate | None = None,
                      chunk_minutes: int = DEFAULT_CHUNK_MINUTES, model: str = DEFAULT_MODEL,
                      base_url: str = DEFAULT_OLLAMA_URL) -> str:
    """Reads `transcript.txt`, writes `summary.md` and returns the summary text."""
    directory = Path(meeting_dir)
    path = directory / "transcript.txt"
    try:
        transcript = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SummaryError(f"cannot read {path}: {exc}") from exc
    generate = generate or make_ollama_generate(model=model, base_url=base_url)

    chunks = chunk_lines(parse_transcript(transcript), chunk_minutes)
    if not chunks:
        raw = ""
    elif len(chunks) == 1:
        raw = _call(generate, final_prompt(_text(chunks[0])), "the transcript")
    else:
        notes = [_call(generate, extract_prompt(_text(chunk), i, len(chunks)), f"part {i} of {len(chunks)}")
                 for i, chunk in enumerate(chunks, 1)]
        raw = _call(generate, merge_prompt(notes), "the merge pass")

    summary = ensure_headings(raw)
    (directory / "summary.md").write_text(summary + "\n", encoding="utf-8")
    return summary


def _text(chunk: list[Line]) -> str:
    return "\n".join(line.text for line in chunk)


# --- Discord ------------------------------------------------------------------------------------------------------

SEPARATORS = ("\n\n", "\n", " ")


def split_for_discord(text: str, limit: int = 2000) -> list[str]:
    """Splits into messages of at most `limit` characters, preferring headings and paragraphs, then lines,
    then spaces, and cutting a single overlong word only as a last resort."""
    text = text.strip()
    return [p for p in _split(text, limit, 0) if p.strip()] if text else []


def _split(text: str, limit: int, level: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    if level == len(SEPARATORS):
        return [text[i:i + limit] for i in range(0, len(text), limit)]
    sep = SEPARATORS[level]
    messages: list[str] = []
    current = ""
    for piece in text.split(sep):
        candidate = f"{current}{sep}{piece}" if current else piece
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            messages.append(current)
        if len(piece) <= limit:
            current = piece
        else:
            *full, current = _split(piece, limit, level + 1)
            messages.extend(full)
    if current:
        messages.append(current)
    return messages
