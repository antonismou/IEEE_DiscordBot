"""Summary stage of the meeting job: transcript.txt in, a Greek summary.md out.

It runs only after the meeting has ended. The transcript (lines `[hh:mm:ss] Name: text`) is split into fixed
windows of `chunk_minutes` by timestamp: window k holds the lines from k*chunk_minutes up to (k+1)*chunk_minutes, so
a window means the same lines on every run. Each window gets an extraction pass (topics, decisions, tasks, open
questions) whose notes are saved in `notes/<minutes>min/<k>.md` as soon as they are made, so a run killed during the
long summary (or retried after a failure) only extracts the windows still missing. A merge pass then writes the
summary, in groups when the notes are too long for one call. A transcript inside one window that fits one call is
summarized directly.

Everything sent to the model must fit its 8192-token context together with the answer, so text is measured with
`estimate_tokens` and a window too long for one call is extracted in parts. Every model call goes through
`generate(prompt)`, which defaults to a local Ollama server, so tests inject a fake. Meeting status is left to the
caller.
"""
from __future__ import annotations

import json
import math
import os
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
# The context holds prompt and answer together. Notes and summaries are a few hundred tokens; keep 1500 free.
OUTPUT_RESERVE = 1500
# Transcript tokens per extraction call: with the instructions (under 700) and the answer this stays below NUM_CTX.
EXTRACT_BUDGET = 5500
MERGE_BUDGET = 5500  # notes tokens per merge call, by the same reckoning
CHARS_PER_TOKEN = 2.5  # Greek: about 6.5 characters and 2.2 Gemma tokens per word, so this overestimates by ~20%
NOTES_DIR = "notes"
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


def windows(lines: list[Line], chunk_minutes: int = DEFAULT_CHUNK_MINUTES) -> dict[int, list[Line]]:
    """The non-empty windows by number, in order: window k holds the lines from k*W up to (k+1)*W seconds."""
    width = chunk_minutes * 60
    found: dict[int, list[Line]] = {}
    for line in lines:
        found.setdefault(line.seconds // width, []).append(line)
    return dict(sorted(found.items()))


def chunk_lines(lines: list[Line], chunk_minutes: int = DEFAULT_CHUNK_MINUTES) -> list[list[Line]]:
    """The lines of each non-empty window, in order."""
    return list(windows(lines, chunk_minutes).values())


# --- context size -------------------------------------------------------------------------------------------------

def estimate_tokens(text: str) -> int:
    """A deliberately high guess at Gemma's token count: every digit is one token (Gemma splits numbers, and the
    labels of speakers with no name yet carry 18-digit Discord ids), everything else 2.5 characters per token."""
    digits = sum(c.isdigit() for c in text)
    return digits + math.ceil((len(text) - digits) / CHARS_PER_TOKEN)


def split_to_fit(lines: list[Line], budget: int = EXTRACT_BUDGET) -> list[list[Line]]:
    """Consecutive lines in parts of at most `budget` estimated tokens; a line too long by itself is cut between
    words (its pieces keep its timestamp)."""
    parts: list[list[Line]] = []
    current: list[Line] = []
    size = 0
    for line in (piece for line in lines for piece in _cut(line, budget)):
        cost = estimate_tokens(line.text) + (1 if current else 0)  # the newline that joins it
        if current and size + cost > budget:
            parts.append(current)
            current, size, cost = [], 0, estimate_tokens(line.text)
        current.append(line)
        size += cost
    if current:
        parts.append(current)
    return parts


def _cut(line: Line, budget: int) -> list[Line]:
    if estimate_tokens(line.text) <= budget:
        return [line]
    pieces: list[str] = []
    for word in line.text.split(" "):
        candidate = f"{pieces[-1]} {word}" if pieces else word
        if pieces and estimate_tokens(candidate) <= budget:
            pieces[-1] = candidate
        else:
            pieces.append(word)  # a single word over the budget cannot happen with real speech
    return [Line(line.seconds, piece) for piece in pieces]


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


def extract_prompt(chunk_text: str, start: int, end: int, part: int = 1, total: int = 1) -> str:
    """The notes for the window from `start` to `end` seconds; `part` of `total` when the window is too long for
    one call."""
    piece = f", μέρος {part} από {total}" if total > 1 else ""
    return (
        f"Παρακάτω είναι ένα τμήμα της απομαγνητοφώνησης μιας συνάντησης, από {_clock(start)} έως {_clock(end)}"
        f"{piece}. Κάθε γραμμή είναι «[ώρα] Όνομα: λόγια». Κράτησε σημειώσεις για αυτό το τμήμα με τις ενότητες "
        "Θέματα, Αποφάσεις, Εργασίες (με υπεύθυνο και προθεσμία) και Ανοιχτά θέματα.\n\n"
        f"{RULES}\nΑπομαγνητοφώνηση:\n{chunk_text}\n"
    )


def _clock(seconds: int) -> str:
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


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

    lines = parse_transcript(transcript)
    by_window = windows(lines, chunk_minutes)
    if not by_window:
        raw = ""
    elif len(by_window) == 1 and estimate_tokens(_text(lines)) <= EXTRACT_BUDGET:
        raw = _call(generate, final_prompt(_text(lines)), "the transcript")
    else:
        speakers = _speakers(directory)
        notes = []
        for k, chunk in by_window.items():
            path = notes_path(directory, k, chunk_minutes)
            text = path.read_text(encoding="utf-8") if path.is_file() else \
                extract_window(directory, k, chunk, generate=generate, chunk_minutes=chunk_minutes)
            notes.append(with_names(text.strip(), speakers))
        raw = merge_notes(generate, notes)

    summary = ensure_headings(raw)
    (directory / "summary.md").write_text(summary + "\n", encoding="utf-8")
    return summary


def _text(chunk: list[Line]) -> str:
    return "\n".join(line.text for line in chunk)


# --- notes per window ---------------------------------------------------------------------------------------------

def notes_path(meeting_dir: Path | str, k: int, chunk_minutes: int = DEFAULT_CHUNK_MINUTES) -> Path:
    """Kept per window size, so notes made with another CHUNK_MINUTES are never mixed in."""
    return Path(meeting_dir) / NOTES_DIR / f"{chunk_minutes}min" / f"{k:03d}.md"


def extract_window(meeting_dir: Path | str, k: int, lines: list[Line], generate: Generate,
                   chunk_minutes: int = DEFAULT_CHUNK_MINUTES, budget: int = EXTRACT_BUDGET) -> str:
    """Makes the notes for window `k` (`lines` are its lines) and saves them. A window too long for one call is
    extracted in parts whose notes go into the same file. Nothing is saved unless every part succeeded."""
    start, end = k * chunk_minutes * 60, (k + 1) * chunk_minutes * 60
    parts = split_to_fit(lines, budget)
    notes = [
        _call(generate, extract_prompt(_text(part), start, end, i, len(parts)),
              f"minutes {start // 60}-{end // 60}" + (f", part {i} of {len(parts)}" if len(parts) > 1 else ""))
        for i, part in enumerate(parts, 1)
    ]
    text = "\n\n".join(n.strip() for n in notes)
    path = notes_path(meeting_dir, k, chunk_minutes)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(text + "\n", encoding="utf-8")
    os.replace(temporary, path)  # a half-written file must never look finished, or the resume would trust it
    return text


def _speakers(directory: Path) -> dict[int, str]:
    try:
        data = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        return {int(user_id): name for user_id, name in data.get("speakers", {}).items() if name}
    except (OSError, ValueError):
        return {}


def with_names(text: str, speakers: dict[int, str]) -> str:
    """Notes saved before a speaker's name was known (a meeting named after a bot restart, notes kept from an
    earlier run) call people «Ομιλητής <id>»; the merge pass gets the names from the manifest instead."""
    for user_id, name in speakers.items():
        text = re.sub(rf"ομιλητ\w*\s+{user_id}(?!\d)", name, text, flags=re.IGNORECASE)
        if len(str(user_id)) >= 15:  # a bare Discord id (17-20 digits) cannot be an amount or a date
            text = re.sub(rf"(?<!\d){user_id}(?!\d)", name, text)
    return text


# --- merging --------------------------------------------------------------------------------------------------

def group_notes(notes: list[str], budget: int = MERGE_BUDGET) -> list[list[str]]:
    """Consecutive notes in groups of at most `budget` estimated tokens. There are always fewer groups than notes
    (oversized ones are paired anyway), so merging group by group always ends."""
    groups: list[list[str]] = []
    size = 0
    for note in notes:
        cost = estimate_tokens(note) + 10  # the part's heading
        if groups and size + cost <= budget:
            groups[-1].append(note)
            size += cost
        else:
            groups.append([note])
            size = cost
    if len(notes) > 1 and len(groups) == len(notes):
        groups = [notes[i:i + 2] for i in range(0, len(notes), 2)]
    return groups


def merge_notes(generate: Generate, notes: list[str], budget: int = MERGE_BUDGET) -> str:
    """One merge pass when the notes fit; otherwise each group is merged into a summary and the summaries are
    merged the same way, until one call holds them all. Every level uses the same merge prompt and rules."""
    level = 1
    while True:
        groups = group_notes(notes, budget)
        if len(groups) == 1:
            return _call(generate, merge_prompt(groups[0]), "the merge pass")
        notes = [ensure_headings(_call(generate, merge_prompt(group), f"merge level {level}, group {i}"))
                 for i, group in enumerate(groups, 1)]
        level += 1


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
