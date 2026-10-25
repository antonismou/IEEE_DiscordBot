#!/usr/bin/env bash
# Compares Greek summary models (Gemma 3 12B, Gemma 3 4B, Llama-Krikri-8B) in throwaway containers,
# saves the output, then removes every container, volume and image.
#
#   scripts/run_summary_benchmark.sh                              # all three models, built-in sample meeting
#   TRANSCRIPT=/path/to/transcript.txt scripts/run_summary_benchmark.sh
#   MODELS="gemma3:4b krikri" scripts/run_summary_benchmark.sh    # a subset
#
# Needs about 16 GB of free disk while running (all three models). Output also goes to
# summary-benchmark-<timestamp>.txt in the current directory.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE=(docker compose -f "$HERE/benchmark.compose.yml" --profile bench --profile summary)
MODELS="${MODELS:-gemma3:12b gemma3:4b krikri}"
OUT="summary-benchmark-$(date +%Y%m%d-%H%M%S).txt"

declare -A TAGS=(
  [gemma3:12b]="gemma3:12b"
  [gemma3:4b]="gemma3:4b"
  [krikri]="hf.co/ilsp/Llama-Krikri-8B-Instruct-GGUF:Q4_K_M"
)

args=()
for m in $MODELS; do args+=(--model "$m"); done

if [[ -n "${TRANSCRIPT:-}" ]]; then
  [[ -f "$TRANSCRIPT" ]] || { echo "TRANSCRIPT file not found: $TRANSCRIPT" >&2; exit 1; }
  export BENCH_TRANSCRIPT_DIR="$(cd "$(dirname "$TRANSCRIPT")" && pwd)"
  args+=(--transcript "/data/$(basename "$TRANSCRIPT")")
fi

cleanup() {
  echo
  echo ">> removing containers, volumes and images"
  "${COMPOSE[@]}" down -v --rmi all --remove-orphans >/dev/null 2>&1 || true
  echo ">> cleaned up. results: $OUT"
}
trap cleanup EXIT

"${COMPOSE[@]}" up -d ollama
for m in $MODELS; do
  tag="${TAGS[$m]:-$m}"
  echo ">> pulling $tag"
  "${COMPOSE[@]}" exec -T ollama ollama pull "$tag"
done

echo ">> running benchmark"
"${COMPOSE[@]}" run --rm -T summary "${args[@]}" 2>&1 | tee "$OUT"
