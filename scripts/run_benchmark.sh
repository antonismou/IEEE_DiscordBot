#!/usr/bin/env bash
# One-shot benchmark: starts the containers, runs the benchmark, saves the output, removes everything.
#
#   scripts/run_benchmark.sh                          # synthetic Greek sample, whisper + gemma3:12b
#   scripts/run_benchmark.sh --skip-llm               # transcription only (no Ollama, no 8 GB model)
#   AUDIO=/path/to/call.wav scripts/run_benchmark.sh  # real recording
#   LLM=qwen2.5:7b scripts/run_benchmark.sh           # other Ollama model
#   scripts/run_benchmark.sh --threads 8 --whisper large-v3   # extra args go to benchmark_models.py
#
# The output is also saved to benchmark-results-<timestamp>.txt in the current directory.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE=(docker compose -f "$HERE/benchmark.compose.yml" --profile bench)
LLM="${LLM:-gemma3:12b}"
OUT="benchmark-results-$(date +%Y%m%d-%H%M%S).txt"

args=("$@")
skip_llm=0
for a in "${args[@]}"; do [[ "$a" == "--skip-llm" ]] && skip_llm=1; done

if [[ -n "${AUDIO:-}" ]]; then
  [[ -f "$AUDIO" ]] || { echo "AUDIO file not found: $AUDIO" >&2; exit 1; }
  export BENCH_AUDIO_DIR="$(cd "$(dirname "$AUDIO")" && pwd)"
  args+=(--audio "/audio/$(basename "$AUDIO")")
else
  args+=(--generate-sample)
fi
if [[ $skip_llm -eq 0 ]]; then
  args+=(--llm "$LLM")
fi

cleanup() {
  echo
  echo ">> removing containers, volumes and images"
  "${COMPOSE[@]}" down -v --rmi all --remove-orphans >/dev/null 2>&1 || true
  echo ">> cleaned up. results: $OUT"
}
trap cleanup EXIT

if [[ $skip_llm -eq 0 ]]; then
  echo ">> starting ollama and pulling $LLM (large download on first run)"
  "${COMPOSE[@]}" up -d ollama
  "${COMPOSE[@]}" exec -T ollama ollama pull "$LLM"
fi

echo ">> running benchmark"
"${COMPOSE[@]}" run --rm -T bench "${args[@]}" 2>&1 | tee "$OUT"

if [[ $skip_llm -eq 0 ]]; then
  { echo; echo "=== ollama log (last 40 lines) ==="; "${COMPOSE[@]}" logs --no-log-prefix --tail 40 ollama 2>&1; } | tee -a "$OUT"
fi
