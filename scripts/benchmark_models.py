"""Benchmark Greek transcription + summary models. Run on the server, outside the bot:

    pip install faster-whisper requests edge-tts   # plus ffmpeg on PATH
    python scripts/benchmark_models.py --generate-sample
    python scripts/benchmark_models.py --audio meeting.wav --llm gemma3:12b --llm krikri

With --generate-sample a short Greek clip is synthesized with edge-tts (needs internet, free),
so the transcript can be scored against the known text. A real recording is a better test:
pass it with --audio (any format ffmpeg/PyAV can read) and optionally --reference ref.txt.

LLMs are called through a local Ollama server (default http://localhost:11434).
Whisper models are loaded one at a time and released, so peak RAM is a single model.
"""
from __future__ import annotations

import argparse
import asyncio
import gc
import re
import sys
import time
from pathlib import Path

import requests

SAMPLE_TEXT = (
    "Καλησπέρα σε όλους, ξεκινάμε με το event της επόμενης εβδομάδας. "
    "Έχουμε κλείσει την αίθουσα για την Πέμπτη στις έξι το απόγευμα. "
    "Ο Νίκος θα αναλάβει τα εισιτήρια και τις εγγραφές μέχρι την Τρίτη. "
    "Η Μαρία θα στείλει το email στα μέλη αύριο το πρωί. "
    "Δεν έχουμε αποφασίσει ακόμα ποιος θα φέρει τον εξοπλισμό, "
    "οπότε θα το συζητήσουμε στην επόμενη συνάντηση. "
    "Επίσης πρέπει να ενημερώσουμε το IEEE Student Branch για το workshop τον Νοέμβριο."
)

SUMMARY_PROMPT = (
    "Παρακάτω είναι η απομαγνητοφώνηση μιας συνάντησης. Γράψε τη σύνοψη στα ελληνικά "
    "με αυτή τη δομή: Θέματα, Αποφάσεις, Εργασίες (με υπεύθυνο και προθεσμία), Ανοιχτά θέματα. "
    "Μην προσθέτεις πληροφορίες που δεν υπάρχουν στο κείμενο.\n\n{transcript}"
)

LLM_ALIASES = {
    "gemma3:12b": "gemma3:12b",
    "qwen": "qwen2.5:7b",
    # Import a community GGUF of Llama-Krikri-8B-Instruct with a Modelfile and name it "krikri".
    "krikri": "krikri",
}


def normalize(text: str) -> list[str]:
    return re.sub(r"[^\w\s]", " ", text.lower()).split()


def wer(reference: str, hypothesis: str) -> float:
    ref, hyp = normalize(reference), normalize(hypothesis)
    if not ref:
        return 0.0
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i]
        for j, h in enumerate(hyp, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h)))
        prev = cur
    return prev[-1] / len(ref)


def generate_sample(path: Path) -> None:
    import edge_tts

    async def run() -> None:
        await edge_tts.Communicate(SAMPLE_TEXT, "el-GR-AthinaNeural").save(str(path))

    asyncio.run(run())
    print(f"sample written to {path}")


def load_audio(path: Path):
    """Decode to 16 kHz mono float32 with ffmpeg, which avoids faster-whisper's PyAV decoding."""
    import subprocess

    import numpy as np

    raw = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-f", "s16le", "-ac", "1", "-ar", "16000", "-"],
        check=True, capture_output=True,
    ).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0


def bench_whisper(model_name: str, audio, threads: int, reference: str | None) -> str:
    from faster_whisper import WhisperModel

    print(f"\n=== whisper {model_name} (cpu, int8, {threads} threads) ===")
    t0 = time.perf_counter()
    model = WhisperModel(model_name, device="cpu", compute_type="int8", cpu_threads=threads)
    load = time.perf_counter() - t0

    duration = len(audio) / 16000
    t0 = time.perf_counter()
    segments, _ = model.transcribe(audio, language="el", vad_filter=True, beam_size=5)
    text = " ".join(s.text.strip() for s in segments)
    took = time.perf_counter() - t0

    print(f"load {load:.1f}s | transcribe {took:.1f}s for {duration:.1f}s audio "
          f"= {took / duration:.2f}x realtime (>1 means slower than the audio)")
    if reference:
        print(f"WER vs reference: {wer(reference, text) * 100:.1f}%")
    print(f"text: {text[:400]}{'...' if len(text) > 400 else ''}")
    del model
    gc.collect()
    return text


def bench_llm(model: str, transcript: str, url: str) -> None:
    print(f"\n=== llm {model} ===")
    try:
        t0 = time.perf_counter()
        r = requests.post(
            f"{url}/api/generate",
            json={
                "model": model,
                "prompt": SUMMARY_PROMPT.format(transcript=transcript),
                "stream": False,
                "keep_alive": 0,
                "options": {"temperature": 0.2, "num_ctx": 8192},
            },
            timeout=3600,
        )
        r.raise_for_status()
    except requests.RequestException as exc:
        print(f"failed: {exc} (is the model pulled? `ollama pull {model}`)")
        return
    took = time.perf_counter() - t0
    data = r.json()
    tokens = data.get("eval_count", 0)
    gen_s = data.get("eval_duration", 0) / 1e9
    rate = f"{tokens / gen_s:.1f} tok/s" if gen_s else "n/a"
    print(f"total {took:.1f}s | {tokens} tokens | {rate}")
    print(data.get("response", "").strip())


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--audio", type=Path, help="audio file to transcribe")
    p.add_argument("--reference", type=Path, help="text file with the true transcript (enables WER)")
    p.add_argument("--generate-sample", action="store_true", help="synthesize a Greek test clip (sample.mp3)")
    p.add_argument("--whisper", action="append", help="whisper model, repeatable (default: large-v3, large-v3-turbo)")
    p.add_argument("--llm", action="append", help="ollama model or alias (gemma3:12b, qwen, krikri), repeatable")
    p.add_argument("--threads", type=int, default=4, help="CPU threads for whisper (default 4)")
    p.add_argument("--ollama-url", default="http://localhost:11434")
    p.add_argument("--skip-llm", action="store_true")
    args = p.parse_args()

    audio, reference = args.audio, None
    if args.generate_sample:
        audio = audio or Path("sample.mp3")
        generate_sample(audio)
        reference = SAMPLE_TEXT
    if audio is None:
        p.error("pass --audio FILE or --generate-sample")
    if args.reference:
        reference = args.reference.read_text(encoding="utf-8")

    samples = load_audio(audio)
    transcript = ""
    for name in args.whisper or ["large-v3", "large-v3-turbo"]:
        text = bench_whisper(name, samples, args.threads, reference)
        transcript = transcript or text

    if not args.skip_llm:
        # Summaries are judged on the clean reference when we have one, so LLM quality
        # is not confounded with transcription errors.
        source = reference or transcript
        for name in args.llm or ["gemma3:12b"]:
            bench_llm(LLM_ALIASES.get(name, name), source, args.ollama_url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
