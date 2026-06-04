#!/usr/bin/env bash
# One command to process all CCTV clips and feed events into the API.
# Usage: ./run.sh [--clips <dir>] [--api <url>] [--simulate]
#
# If no clips directory is provided, or if no video files are found,
# the simulator generates synthetic events from store_layout.json.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLIPS_DIR="${CLIPS_DIR:-./clips}"
OUTPUT_FILE="${OUTPUT_FILE:-./events.jsonl}"
API_URL="${API_URL:-http://localhost:8000}"
SIMULATE="${SIMULATE:-false}"

# Parse args
while [[ $# -gt 0 ]]; do
  case $1 in
    --clips) CLIPS_DIR="$2"; shift 2 ;;
    --output) OUTPUT_FILE="$2"; shift 2 ;;
    --api) API_URL="$2"; shift 2 ;;
    --simulate) SIMULATE="true"; shift ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

echo "=== Store Intelligence Detection Pipeline ==="
echo "Clips dir : $CLIPS_DIR"
echo "Output    : $OUTPUT_FILE"
echo "API URL   : $API_URL"

cd "$SCRIPT_DIR"

if [[ "$SIMULATE" == "true" ]]; then
  echo "[run] Simulation mode forced."
  python simulator.py "$OUTPUT_FILE"
elif [[ -d "$CLIPS_DIR" ]] && compgen -G "$CLIPS_DIR/**/*.mp4" > /dev/null 2>&1; then
  echo "[run] Found video clips, running YOLOv8 detection..."
  python detect.py --clips "$CLIPS_DIR" --output "$OUTPUT_FILE" --api "$API_URL"
else
  echo "[run] No video clips found in $CLIPS_DIR — running simulator..."
  python simulator.py "$OUTPUT_FILE"
fi

echo "[run] Events written to $OUTPUT_FILE"
echo "[run] Ingesting into API at $API_URL ..."

# Wait for API to be ready (up to 30s)
for i in $(seq 1 30); do
  if curl -sf "$API_URL/health" > /dev/null 2>&1; then
    break
  fi
  echo "[run] Waiting for API... ($i/30)"
  sleep 1
done

# Batch-ingest events.jsonl → API
python - <<'EOF'
import json, sys, requests, os

api_url = os.environ.get("API_URL", "http://localhost:8000")
output_file = os.environ.get("OUTPUT_FILE", "./events.jsonl")

events = []
with open(output_file) as f:
    for line in f:
        line = line.strip()
        if line:
            events.append(json.loads(line))

BATCH = 500
total_accepted = 0
for i in range(0, len(events), BATCH):
    batch = events[i:i+BATCH]
    try:
        r = requests.post(f"{api_url}/events/ingest", json=batch, timeout=30)
        if r.status_code == 200:
            res = r.json()
            total_accepted += res.get("accepted", 0)
            print(f"Batch {i//BATCH + 1}: accepted={res.get('accepted')}, "
                  f"duplicates={res.get('duplicates')}, rejected={res.get('rejected')}")
        else:
            print(f"Batch {i//BATCH + 1}: HTTP {r.status_code} — {r.text[:200]}")
    except Exception as e:
        print(f"Batch {i//BATCH + 1}: Error — {e}")

print(f"Done. Total accepted: {total_accepted}/{len(events)}")
EOF

echo "[run] Pipeline complete."
