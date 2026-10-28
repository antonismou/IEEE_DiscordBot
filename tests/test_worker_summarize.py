import pytest
import requests

from worker.summarize import (
    EMPTY_SECTION,
    EXTRACT_BUDGET,
    HEADINGS,
    MERGE_BUDGET,
    NUM_CTX,
    OUTPUT_RESERVE,
    OllamaError,
    SummaryError,
    chunk_lines,
    ensure_headings,
    estimate_tokens,
    extract_prompt,
    extract_window,
    final_prompt,
    group_notes,
    make_ollama_generate,
    merge_notes,
    merge_prompt,
    notes_path,
    parse_transcript,
    split_for_discord,
    split_to_fit,
    summarize_meeting,
    windows,
    with_names,
)

GOOD_SUMMARY = (
    "## Θέματα\n- Workshop Arduino\n\n## Αποφάσεις\n- Εγγραφές με φόρμα\n\n"
    "## Εργασίες\n- Μαρία: φόρμα, μέχρι Παρασκευή\n\n## Ανοιχτά θέματα\n- Ομιλητής"
)


def line(seconds, name="Μαρία", text="καλησπέρα"):
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"[{h:02d}:{m:02d}:{s:02d}] {name}: {text}"


def write_transcript(directory, lines):
    (directory / "transcript.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


class Recorder:
    """A fake generate: returns canned answers in order and records every prompt."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        answer = self.answers.pop(0) if self.answers else GOOD_SUMMARY
        if isinstance(answer, Exception):
            raise answer
        return answer


# --- parsing and chunking ---------------------------------------------------------------------------------------

def test_transcript_lines_are_parsed_with_their_offset_in_seconds():
    lines = parse_transcript(line(4) + "\n" + line(3725, "Νίκος", "300 ευρώ") + "\n")
    assert [(l.seconds, l.text) for l in lines] == [(4, line(4)), (3725, line(3725, "Νίκος", "300 ευρώ"))]


def test_a_line_without_a_timestamp_stays_with_the_line_before_it():
    lines = parse_transcript(line(4) + "\nσυνέχεια της πρότασης\n\n" + line(10))
    assert len(lines) == 2 and lines[0].text == line(4) + "\nσυνέχεια της πρότασης"


def test_an_empty_transcript_has_no_lines_and_no_chunks():
    assert parse_transcript("") == [] and parse_transcript("\n  \n") == []
    assert chunk_lines([], chunk_minutes=12) == []


def test_a_one_line_transcript_is_one_chunk():
    lines = parse_transcript(line(4))
    assert chunk_lines(lines, chunk_minutes=12) == [lines]


def test_chunks_start_a_new_piece_once_the_time_window_is_reached():
    lines = parse_transcript("\n".join(line(s) for s in [0, 300, 719, 720, 1000, 1440, 1500]))
    chunks = chunk_lines(lines, chunk_minutes=12)
    assert [[l.seconds for l in chunk] for chunk in chunks] == [[0, 300, 719], [720, 1000], [1440, 1500]]


def test_windows_are_fixed_by_absolute_time_not_by_the_first_line():
    # so a window found complete during the call is the same window after it, whatever came before
    lines = parse_transcript("\n".join(line(s) for s in [100, 800, 900, 1500, 1520, 1620, 4400]))
    chunks = chunk_lines(lines, chunk_minutes=12)
    assert [[l.seconds for l in chunk] for chunk in chunks] == [[100], [800, 900], [1500, 1520, 1620], [4400]]
    assert sorted(windows(lines, chunk_minutes=12)) == [0, 1, 2, 6]  # window k covers [k*12 min, (k+1)*12 min)


def test_chunking_never_splits_or_loses_a_line():
    text = "\n".join(line(s, text=f"πρόταση {s} " * 50) for s in range(0, 3600, 37))
    lines = parse_transcript(text)
    chunks = chunk_lines(lines, chunk_minutes=10)
    assert [l for chunk in chunks for l in chunk] == lines
    assert all(chunk for chunk in chunks)


# --- prompts ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("prompt", [
    extract_prompt("[00:00:04] Μαρία: γεια", start=0, end=720),
    merge_prompt(["σημειώσεις 1", "σημειώσεις 2"]),
    final_prompt("[00:00:04] Μαρία: γεια"),
])
def test_every_prompt_carries_the_guard_instructions(prompt):
    assert "Μην προσθέτεις τίποτα που δεν υπάρχει στο κείμενο" in prompt
    assert "ΜΟΝΟ εργασίες που ανατέθηκαν ρητά" in prompt
    assert "γράψε μόνο την τελική" in prompt
    assert "Ανοιχτά θέματα" in prompt and "αναβλήθηκαν" in prompt


def test_the_final_prompts_ask_for_the_four_headings_in_order():
    for prompt in (merge_prompt(["σημειώσεις"]), final_prompt("κείμενο")):
        positions = [prompt.index(f"## {h}\n") for h in HEADINGS]
        assert positions == sorted(positions)


def test_prompts_contain_their_input():
    assert "[00:00:04] Μαρία: γεια" in extract_prompt("[00:00:04] Μαρία: γεια", start=0, end=720)
    assert "από 00:12:00 έως 00:24:00" in extract_prompt("x", start=720, end=1440)
    assert "μέρος 2 από 3" in extract_prompt("x", start=720, end=1440, part=2, total=3)
    assert "μέρος" not in extract_prompt("x", start=720, end=1440)
    merged = merge_prompt(["πρώτες σημειώσεις", "δεύτερες σημειώσεις"])
    assert merged.index("πρώτες σημειώσεις") < merged.index("δεύτερες σημειώσεις")


# --- headings ---------------------------------------------------------------------------------------------------

def test_a_well_formed_summary_is_kept_as_it_is():
    assert ensure_headings(GOOD_SUMMARY) == GOOD_SUMMARY


def test_missing_and_empty_sections_get_a_placeholder():
    fixed = ensure_headings("## Θέματα\n- Workshop\n\n## Εργασίες\n")
    assert fixed == (
        f"## Θέματα\n- Workshop\n\n## Αποφάσεις\n{EMPTY_SECTION}\n\n"
        f"## Εργασίες\n{EMPTY_SECTION}\n\n## Ανοιχτά θέματα\n{EMPTY_SECTION}"
    )


def test_loose_heading_styles_are_recognized_and_reordered_and_preamble_dropped():
    raw = (
        "Ακολουθεί η σύνοψη:\n\n**Ανοιχτά Θέματα:**\n- Ομιλητής\n\n### 1. Θέματα\n- Workshop\n\n"
        "Εργασίες (με υπεύθυνο και προθεσμία):\n- Ελένη: αφίσα\n\n## Αποφάσεις\n- Φόρμα"
    )
    assert ensure_headings(raw) == (
        "## Θέματα\n- Workshop\n\n## Αποφάσεις\n- Φόρμα\n\n## Εργασίες\n- Ελένη: αφίσα\n\n"
        "## Ανοιχτά θέματα\n- Ομιλητής"
    )


def test_text_without_any_heading_becomes_topics_so_nothing_is_lost():
    fixed = ensure_headings("Μιλήσαμε για το workshop.")
    assert fixed.startswith("## Θέματα\nΜιλήσαμε για το workshop.\n\n## Αποφάσεις\n")
    assert all(f"## {h}" in fixed for h in HEADINGS)


# --- the Ollama client ------------------------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.ok = status < 400
        self.text = str(body)

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def post(self, url, json, timeout):
        self.calls.append((url, json))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_the_client_sends_the_configured_model_and_options():
    session = FakeSession(FakeResponse(200, {"response": "  απάντηση \n"}))
    generate = make_ollama_generate(model="gemma3:12b", base_url="http://ollama:11434/", session=session)

    assert generate("ερώτηση") == "απάντηση"
    url, body = session.calls[0]
    assert url == "http://ollama:11434/api/generate"
    assert body == {
        "model": "gemma3:12b", "prompt": "ερώτηση", "stream": False, "keep_alive": 0,
        "options": {"temperature": 0.2, "num_ctx": 8192},
    }


def test_the_client_turns_http_and_connection_errors_into_ollama_errors():
    generate = make_ollama_generate(session=FakeSession(FakeResponse(500, {"error": "boom"})))
    with pytest.raises(OllamaError, match="500"):
        generate("x")
    generate = make_ollama_generate(session=FakeSession(requests.ConnectionError("refused")))
    with pytest.raises(OllamaError, match="refused"):
        generate("x")


# --- the whole stage --------------------------------------------------------------------------------------------

def test_a_short_meeting_is_summarized_in_one_call_and_written_to_summary_md(tmp_path):
    write_transcript(tmp_path, [line(4), line(60, "Νίκος", "300 ευρώ")])
    generate = Recorder(GOOD_SUMMARY)

    summary = summarize_meeting(tmp_path, generate=generate)

    assert summary == GOOD_SUMMARY
    assert (tmp_path / "summary.md").read_text(encoding="utf-8") == GOOD_SUMMARY + "\n"
    assert len(generate.prompts) == 1 and "300 ευρώ" in generate.prompts[0]


def test_a_long_meeting_is_extracted_per_chunk_then_merged(tmp_path):
    write_transcript(tmp_path, [line(0, text="α"), line(800, text="β"), line(1600, text="γ")])
    generate = Recorder("σημειώσεις α", "σημειώσεις β", "σημειώσεις γ", GOOD_SUMMARY)

    summary = summarize_meeting(tmp_path, generate=generate, chunk_minutes=12)

    assert summary == GOOD_SUMMARY
    assert len(generate.prompts) == 4
    assert "Μαρία: α" in generate.prompts[0] and "από 00:00:00 έως 00:12:00" in generate.prompts[0]
    assert "Μαρία: γ" in generate.prompts[2] and "από 00:24:00 έως 00:36:00" in generate.prompts[2]
    assert all(f"σημειώσεις {x}" in generate.prompts[3] for x in "αβγ")


def test_the_written_summary_always_has_all_four_headings(tmp_path):
    write_transcript(tmp_path, [line(4)])
    summary = summarize_meeting(tmp_path, generate=Recorder("Θέματα:\n- Workshop"))
    assert [h for h in HEADINGS if f"## {h}\n" in summary] == list(HEADINGS)
    assert summary.count(EMPTY_SECTION) == 3
    assert (tmp_path / "summary.md").read_text(encoding="utf-8") == summary + "\n"


def test_an_empty_transcript_gives_an_empty_summary_without_calling_the_model(tmp_path):
    write_transcript(tmp_path, [])
    generate = Recorder()
    summary = summarize_meeting(tmp_path, generate=generate)
    assert generate.prompts == []
    assert summary.count(EMPTY_SECTION) == 4


def test_an_ollama_error_is_retried_once(tmp_path):
    write_transcript(tmp_path, [line(4)])
    generate = Recorder(OllamaError("HTTP 500"), GOOD_SUMMARY)
    assert summarize_meeting(tmp_path, generate=generate) == GOOD_SUMMARY
    assert len(generate.prompts) == 2 and generate.prompts[0] == generate.prompts[1]


def test_a_second_ollama_error_raises_a_clear_error_and_writes_nothing(tmp_path):
    write_transcript(tmp_path, [line(4)])
    generate = Recorder(OllamaError("HTTP 500"), OllamaError("HTTP 500 again"))
    with pytest.raises(SummaryError, match="failed twice.*HTTP 500 again"):
        summarize_meeting(tmp_path, generate=generate)
    assert len(generate.prompts) == 2
    assert not (tmp_path / "summary.md").exists()


def test_a_missing_transcript_raises_a_clear_error(tmp_path):
    with pytest.raises(SummaryError, match="transcript.txt"):
        summarize_meeting(tmp_path, generate=Recorder())


# --- context size -------------------------------------------------------------------------------------------

GREEK = ("Καλησπέρα σε όλους, σήμερα θα συζητήσουμε για το workshop του Νοεμβρίου και τον προϋπολογισμό "
         "των 300 ευρώ που ζήτησε η σχολή. ")


def test_the_token_estimate_is_conservative_for_greek_and_counts_every_digit():
    words = len(GREEK.split())
    assert estimate_tokens(GREEK) >= 2.4 * words  # measured on Gemma: about 2.2 tokens per Greek word
    assert estimate_tokens("123456789012345678") == 18  # Gemma splits numbers into single digits
    assert estimate_tokens("") == 0


def test_the_budgets_leave_room_for_the_prompt_and_the_answer_inside_the_context():
    assert estimate_tokens(extract_prompt("", start=0, end=720, part=9, total=9)) + EXTRACT_BUDGET \
        + OUTPUT_RESERVE <= NUM_CTX
    assert estimate_tokens(final_prompt("")) + EXTRACT_BUDGET + OUTPUT_RESERVE <= NUM_CTX
    assert estimate_tokens(merge_prompt([""] * 20)) + MERGE_BUDGET + OUTPUT_RESERVE <= NUM_CTX


def test_lines_that_fit_stay_one_part():
    lines = parse_transcript("\n".join(line(s, text=GREEK) for s in range(0, 600, 60)))
    assert split_to_fit(lines, budget=10_000) == [lines]


def test_a_window_too_big_for_the_context_is_split_into_parts_that_fit_without_losing_a_line():
    lines = parse_transcript("\n".join(line(s, text=GREEK * 3) for s in range(0, 720, 10)))
    parts = split_to_fit(lines, budget=1000)
    assert len(parts) > 1
    assert all(estimate_tokens("\n".join(l.text for l in part)) <= 1000 for part in parts)
    assert [l for part in parts for l in part] == lines


def test_a_single_line_too_big_for_the_context_is_cut_between_words():
    huge = parse_transcript(line(30, text=GREEK * 60))
    parts = split_to_fit(huge, budget=500)
    assert len(parts) > 1
    assert all(estimate_tokens("\n".join(l.text for l in part)) <= 500 for part in parts)
    assert " ".join(l.text for part in parts for l in part).split() == huge[0].text.split()
    assert all(l.seconds == 30 for part in parts for l in part)


# --- notes per window -----------------------------------------------------------------------------------------

def test_a_window_is_extracted_once_and_its_notes_kept_on_disk(tmp_path):
    lines = parse_transcript("\n".join([line(800, text="β"), line(900, text="γ")]))
    generate = Recorder("σημειώσεις β")

    assert extract_window(tmp_path, 1, lines, generate=generate, chunk_minutes=12) == "σημειώσεις β"

    path = notes_path(tmp_path, 1, chunk_minutes=12)
    assert path == tmp_path / "notes" / "12min" / "001.md"
    assert path.read_text(encoding="utf-8") == "σημειώσεις β\n"
    assert "από 00:12:00 έως 00:24:00" in generate.prompts[0] and "Μαρία: γ" in generate.prompts[0]
    assert not list(path.parent.glob("*.tmp"))  # written atomically


def test_a_window_split_in_parts_gives_one_notes_file(tmp_path):
    lines = parse_transcript("\n".join(line(s, text=GREEK * 3) for s in range(720, 1440, 10)))
    generate = Recorder("πρώτο μισό", "δεύτερο μισό", "τρίτο")

    notes = extract_window(tmp_path, 1, lines, generate=generate, chunk_minutes=12, budget=3000)

    assert len(generate.prompts) >= 2 and all(estimate_tokens(p) <= 3000 + 1000 for p in generate.prompts)
    assert "μέρος 1 από" in generate.prompts[0]
    assert notes.startswith("πρώτο μισό\n\nδεύτερο μισό")
    assert notes_path(tmp_path, 1, chunk_minutes=12).read_text(encoding="utf-8") == notes + "\n"


def test_a_failed_extraction_writes_no_notes(tmp_path):
    lines = parse_transcript(line(800))
    with pytest.raises(SummaryError):
        extract_window(tmp_path, 1, lines, generate=Recorder(OllamaError("a"), OllamaError("b")), chunk_minutes=12)
    assert not notes_path(tmp_path, 1, chunk_minutes=12).exists()


def test_stored_notes_are_used_and_only_missing_windows_are_extracted(tmp_path):
    write_transcript(tmp_path, [line(0, text="α"), line(800, text="β"), line(1600, text="γ")])
    for k, text in [(0, "σημειώσεις α"), (2, "σημειώσεις γ")]:  # made during the call
        notes_path(tmp_path, k, chunk_minutes=12).parent.mkdir(parents=True, exist_ok=True)
        notes_path(tmp_path, k, chunk_minutes=12).write_text(text + "\n", encoding="utf-8")
    generate = Recorder("σημειώσεις β", GOOD_SUMMARY)

    assert summarize_meeting(tmp_path, generate=generate, chunk_minutes=12) == GOOD_SUMMARY

    assert len(generate.prompts) == 2
    assert "Μαρία: β" in generate.prompts[0] and "Μαρία: α" not in generate.prompts[0]
    merge = generate.prompts[1]
    assert merge.index("σημειώσεις α") < merge.index("σημειώσεις β") < merge.index("σημειώσεις γ")
    assert notes_path(tmp_path, 1, chunk_minutes=12).read_text(encoding="utf-8") == "σημειώσεις β\n"


def test_notes_made_with_another_window_size_are_ignored(tmp_path):
    write_transcript(tmp_path, [line(0, text="α"), line(800, text="β")])
    stale = tmp_path / "notes" / "20min" / "000.md"
    stale.parent.mkdir(parents=True)
    stale.write_text("παλιές σημειώσεις\n", encoding="utf-8")
    generate = Recorder("σημειώσεις α", "σημειώσεις β", GOOD_SUMMARY)

    summarize_meeting(tmp_path, generate=generate, chunk_minutes=12)
    assert len(generate.prompts) == 3 and "παλιές" not in generate.prompts[2]


def test_speaker_ids_in_notes_made_during_the_call_get_the_names_known_after_it():
    speakers = {123456789012345678: "Μαρία", 42: "Νίκος"}
    text = "- Ο Ομιλητής 123456789012345678 ανέλαβε την αφίσα, ο ομιλητή 42 και ο 123456789012345678. 4242"
    assert with_names(text, speakers) == "- Ο Μαρία ανέλαβε την αφίσα, ο Νίκος και ο Μαρία. 4242"


def test_the_merge_pass_sees_names_instead_of_ids(tmp_path):
    write_transcript(tmp_path, [line(0, text="α"), line(800, text="β")])
    (tmp_path / "manifest.json").write_text(
        '{"status": "summarizing", "speakers": {"123456789012345678": "Μαρία"}, "segments": []}', encoding="utf-8")
    path = notes_path(tmp_path, 0, chunk_minutes=12)
    path.parent.mkdir(parents=True)
    path.write_text("- Ομιλητής 123456789012345678: αφίσα\n", encoding="utf-8")
    generate = Recorder("σημειώσεις β", GOOD_SUMMARY)

    summarize_meeting(tmp_path, generate=generate, chunk_minutes=12)
    assert "- Μαρία: αφίσα" in generate.prompts[1] and "123456789012345678" not in generate.prompts[1]
    assert "123456789012345678" in path.read_text(encoding="utf-8")  # the stored notes stay as they were


def test_a_single_window_too_big_for_one_call_is_extracted_in_parts_then_merged(tmp_path):
    write_transcript(tmp_path, [line(s, text=GREEK * 6) for s in range(0, 720, 6)])
    generate = Recorder()

    summarize_meeting(tmp_path, generate=generate, chunk_minutes=12)

    assert len(generate.prompts) >= 3  # at least two parts and the merge pass
    assert all(estimate_tokens(p) + OUTPUT_RESERVE <= NUM_CTX for p in generate.prompts)


# --- hierarchical merge -----------------------------------------------------------------------------------------

def test_notes_that_fit_are_merged_in_one_call():
    generate = Recorder("τελική")
    assert merge_notes(generate, ["α", "β", "γ"]) == "τελική"
    assert len(generate.prompts) == 1


def test_groups_respect_the_budget_keep_the_order_and_always_shrink():
    notes = [f"σημειώσεις {i} " + GREEK * 4 for i in range(10)]
    groups = group_notes(notes, budget=1500)
    assert [n for g in groups for n in g] == notes
    assert 1 < len(groups) < len(notes)
    assert all(len(g) == 1 or sum(estimate_tokens(n) for n in g) <= 1500 for g in groups)
    oversized = [GREEK * 40] * 5  # each alone is over the budget: paired anyway, so the merge still ends
    assert [len(g) for g in group_notes(oversized, budget=100)] == [2, 2, 1]


def test_many_notes_are_merged_in_groups_then_the_groups_are_merged():
    notes = [f"σημειώσεις {i} " + GREEK * 4 for i in range(15)]  # a three-hour meeting
    generate = Recorder()

    assert merge_notes(generate, notes, budget=1500) == GOOD_SUMMARY

    first_level = generate.prompts[:-1]
    assert len(first_level) >= 2
    for i in range(15):  # every note reaches exactly one first-level merge
        assert sum(f"σημειώσεις {i} " in p for p in first_level) == 1
    assert all(estimate_tokens(p) <= 1500 + 1000 for p in generate.prompts)
    assert all(f"## {h}" in generate.prompts[-1] for h in HEADINGS)  # the last pass merges the group summaries
    for prompt in generate.prompts:  # every level keeps the merge rules
        assert "ΜΟΝΟ εργασίες που ανατέθηκαν ρητά" in prompt and "αποφασίστηκε σε επόμενο" in prompt


# --- Discord splitting ------------------------------------------------------------------------------------------

def test_a_short_summary_is_one_message():
    assert split_for_discord(GOOD_SUMMARY) == [GOOD_SUMMARY]
    assert split_for_discord("") == []


def test_messages_break_at_headings_and_paragraphs_and_stay_under_the_limit():
    sections = [f"## {h}\n" + "\n".join(f"- σημείο {i} του {h}" for i in range(8)) for h in HEADINGS]
    text = "\n\n".join(sections)
    parts = split_for_discord(text, limit=300)
    assert all(len(p) <= 300 for p in parts)
    assert all(p.startswith("## ") for p in parts)
    assert "\n\n".join(parts) == text


def test_a_section_longer_than_the_limit_breaks_between_lines():
    text = "## Εργασίες\n" + "\n".join(f"- εργασία αριθμός {i}" for i in range(40))
    parts = split_for_discord(text, limit=200)
    assert len(parts) > 1 and all(len(p) <= 200 for p in parts)
    assert parts[0].startswith("## Εργασίες\n- εργασία αριθμός 0")
    assert "\n".join(parts) == text


def test_a_single_huge_line_is_cut_at_spaces_without_losing_words():
    text = " ".join(["λέξη"] * 1000)
    parts = split_for_discord(text, limit=2000)
    assert all(len(p) <= 2000 for p in parts)
    assert " ".join(parts).split() == text.split()


def test_a_word_longer_than_the_limit_is_cut_hard():
    parts = split_for_discord("α" * 4500, limit=2000)
    assert [len(p) for p in parts] == [2000, 2000, 500]
