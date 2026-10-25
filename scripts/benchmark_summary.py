"""Compare Greek summary models on speed and accuracy. Run through scripts/run_summary_benchmark.sh, or directly:

    python scripts/benchmark_summary.py --model gemma3:12b --model gemma3:4b
    python scripts/benchmark_summary.py --transcript real_transcript.txt --model krikri

Each model gets the same prompt through a local Ollama server. The script reports load time, reading speed
(prompt tokens/s), writing speed (output tokens/s), an estimate for a 1-hour meeting, and a fact check against
the built-in sample meeting. The fact check is a keyword heuristic: it catches missing or wrong facts but cannot
judge style or invented details, so read the printed summaries yourself. With --transcript there is no fact check.
"""
from __future__ import annotations

import argparse
import sys
import time
import unicodedata
from pathlib import Path

import requests

MODELS = {
    "gemma3:12b": "gemma3:12b",
    "gemma3:4b": "gemma3:4b",
    "krikri": "hf.co/ilsp/Llama-Krikri-8B-Instruct-GGUF:Q4_K_M",
}

SUMMARY_PROMPT = (
    "Παρακάτω είναι η απομαγνητοφώνηση μιας συνάντησης. Γράψε τη σύνοψη στα ελληνικά "
    "με αυτή τη δομή: Θέματα, Αποφάσεις, Εργασίες (με υπεύθυνο και προθεσμία), Ανοιχτά θέματα. "
    "Μην προσθέτεις πληροφορίες που δεν υπάρχουν στο κείμενο.\n\n{transcript}"
)

# A 3-minute IEEE-style meeting with traps: a deadline that is corrected mid-sentence, an undecided topic,
# and a topic that is postponed. Day names and dates are consistent with the 2024 calendar.
SAMPLE_TRANSCRIPT = """\
[00:00:04] Γιώργος: Καλησπέρα σε όλους. Σήμερα έχουμε τρία θέματα: το workshop Arduino, τον προϋπολογισμό και τις εγγραφές.
[00:00:15] Μαρία: Να πω πρώτα ότι κλείσαμε την αίθουσα Α2 για την Πέμπτη 21 Νοεμβρίου στις έξι το απόγευμα. Χωράει σαράντα άτομα.
[00:00:28] Γιώργος: Τέλεια. Νίκο, πού βρισκόμαστε με τα χρήματα;
[00:00:34] Νίκος: Το τμήμα μάς έχει εγκρίνει 300 ευρώ. Ένα κιτ Arduino κοστίζει 25 ευρώ, άρα 10 κιτ κάνουν 250. Τα υπόλοιπα 50 τα κρατάμε για καφέδες και εκτυπώσεις.
[00:00:52] Ελένη: Εγώ μπορώ να φτιάξω την αφίσα. Θα την έχω έτοιμη μέχρι τη Δευτέρα 11 Νοεμβρίου.
[00:01:03] Γιώργος: Ωραία, Ελένη. Για τις εγγραφές, να κάνουμε φόρμα ή να έρχονται απλά οι άνθρωποι;
[00:01:12] Μαρία: Καλύτερα φόρμα, για να ξέρουμε πόσα κιτ χρειαζόμαστε. Αν θέλετε, ανοίγω εγώ τη φόρμα μέχρι την Παρασκευή.
[00:01:24] Γιώργος: Συμφωνούμε λοιπόν, θα γίνουν εγγραφές με φόρμα και την ετοιμάζει η Μαρία.
[00:01:32] Νίκος: Τα κιτ πρέπει να τα παραγγείλω μέχρι την Τρίτη 5 Νοεμβρίου για να προλάβουν να έρθουν. Α, όχι, συγγνώμη, μέχρι την Τετάρτη 6 Νοεμβρίου, γιατί ο προμηθευτής παραδίδει σε τρεις μέρες.
[00:01:50] Γιώργος: Ποιος θα κάνει την παρουσίαση στο workshop;
[00:01:55] Ελένη: Μπορούμε να καλέσουμε κάποιον καθηγητή.
[00:02:01] Γιώργος: Έχω στο μυαλό μου έναν καθηγητή από το εργαστήριο ηλεκτρονικής, αλλά δεν του έχω μιλήσει ακόμα, οπότε δεν είναι σίγουρο.
[00:02:14] Μαρία: Άρα το αφήνουμε ανοιχτό.
[00:02:18] Γιώργος: Ναι. Τελευταίο θέμα, τα νέα μέλη. Έχουμε χρόνο σήμερα;
[00:02:25] Νίκος: Όχι, είμαι αργά. Να το πάμε στην επόμενη συνάντηση.
[00:02:30] Γιώργος: Εντάξει, το μεταφέρουμε. Ευχαριστώ όλους, καλή συνέχεια.
"""

# Each fact: (label, groups). Every group needs at least one of its alternatives in the summary.
# Text is compared lowercase with accents removed.
FACTS = [
    ("workshop on 21 November", [["21"], ["νοεμβρ"]]),
    ("room A2", [["α2"]]),
    ("budget 300 euros", [["300"]]),
    ("10 kits", [["10"], ["κιτ"]]),
    ("Eleni makes the poster", [["ελενη"], ["αφισ"]]),
    ("Maria makes the form", [["μαρια"], ["φορμ"]]),
    ("kit order due Wednesday (corrected)", [["τεταρτη", "6 νοεμβρ", "6/11"]]),
    ("presenter left open", [["καθηγητ"], ["ανοιχτ", "δεν ειναι σιγουρ", "δεν εχει επιβεβαι", "δεν εχει αποφασι"]]),
    ("new members postponed", [["μελ"], ["επομεν", "μεταφερ", "αναβλ"]]),
]
WRONG = [("uses the retracted Tuesday", "τριτη")]

# Assumptions for the 1-hour estimate; the real job reads the whole transcript and writes notes plus a merge pass.
HOUR_READ_TOKENS = 17_000
HOUR_WRITE_TOKENS = 2_000


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.replace("ς", "σ")


def check_facts(summary: str) -> tuple[list[str], list[str]]:
    norm = normalize(summary)
    missing = []
    for label, groups in FACTS:
        if not all(any(normalize(alt) in norm for alt in group) for group in groups):
            missing.append(label)
    wrong = [label for label, term in WRONG if normalize(term) in norm]
    return missing, wrong


def run_model(name: str, tag: str, transcript: str, url: str) -> dict:
    print(f"\n{'=' * 70}\n{name}  ({tag})\n{'=' * 70}")
    t0 = time.perf_counter()
    try:
        r = requests.post(
            f"{url}/api/generate",
            json={
                "model": tag,
                "prompt": SUMMARY_PROMPT.format(transcript=transcript),
                "stream": False,
                "keep_alive": 0,
                "options": {"temperature": 0.2, "num_ctx": 8192},
            },
            timeout=3600,
        )
    except requests.RequestException as exc:
        print(f"failed: {exc}")
        return {"name": name, "error": str(exc)}
    if not r.ok:
        print(f"failed: HTTP {r.status_code}: {r.text[:500]}")
        return {"name": name, "error": f"HTTP {r.status_code}"}
    wall = time.perf_counter() - t0
    d = r.json()
    summary = d.get("response", "").strip()
    read_tps = d["prompt_eval_count"] / (d["prompt_eval_duration"] / 1e9) if d.get("prompt_eval_duration") else 0
    write_tps = d["eval_count"] / (d["eval_duration"] / 1e9) if d.get("eval_duration") else 0
    load = d.get("load_duration", 0) / 1e9
    hour_min = (HOUR_READ_TOKENS / read_tps + HOUR_WRITE_TOKENS / write_tps) / 60 if read_tps and write_tps else 0
    print(f"load {load:.1f}s | read {read_tps:.1f} tok/s ({d['prompt_eval_count']} tokens) | "
          f"write {write_tps:.1f} tok/s ({d['eval_count']} tokens) | total {wall:.0f}s")
    print(f"estimated summary time for a 1-hour meeting: {hour_min:.0f} min "
          f"({HOUR_READ_TOKENS} tokens read, {HOUR_WRITE_TOKENS} written)\n")
    print(summary)
    return {"name": name, "load": load, "read": read_tps, "write": write_tps, "hour": hour_min, "summary": summary}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--transcript", type=Path, help="transcript text file (default: built-in Greek sample)")
    p.add_argument("--model", action="append", help=f"model alias or Ollama tag, repeatable (aliases: {', '.join(MODELS)})")
    p.add_argument("--ollama-url", default="http://localhost:11434")
    args = p.parse_args()

    transcript = args.transcript.read_text(encoding="utf-8") if args.transcript else SAMPLE_TRANSCRIPT
    use_facts = args.transcript is None
    results = [run_model(m, MODELS.get(m, m), transcript, args.ollama_url) for m in args.model or list(MODELS)]

    print(f"\n{'=' * 70}\nCOMPARISON\n{'=' * 70}")
    header = f"{'model':<14}{'read tok/s':>11}{'write tok/s':>12}{'1h est.':>9}"
    print(header + (f"{'facts':>8}" if use_facts else ""))
    for res in results:
        if "error" in res:
            print(f"{res['name']:<14}failed ({res['error']})")
            continue
        line = f"{res['name']:<14}{res['read']:>11.1f}{res['write']:>12.1f}{res['hour']:>7.0f} m"
        if use_facts:
            missing, wrong = check_facts(res["summary"])
            res["missing"], res["wrong"] = missing, wrong
            line += f"{len(FACTS) - len(missing):>5}/{len(FACTS)}"
        print(line)
    if use_facts:
        print("\nMissing facts and mistakes per model:")
        for res in results:
            if "error" not in res:
                print(f"- {res['name']}: missing {res['missing'] or 'none'}; wrong {res['wrong'] or 'none'}")
        print("\nThe fact check is a keyword heuristic. Read the summaries above for style and invented details.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
