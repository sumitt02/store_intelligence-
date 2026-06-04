# Store Intelligence — Apex Retail

End-to-end CCTV analytics: raw video → live store metrics API.

## Quick Start (5 commands)

```bash
git clone <repo-url> store-intelligence && cd store-intelligence
docker compose up --build -d
# Wait ~20 seconds for health checks to pass, then seed synthetic data:
docker compose up seed
# Dashboard at http://localhost:3000
# API docs at http://localhost:8000/docs
```

That's it. No manual steps beyond those four commands.

---

## Running the Detection Pipeline Against Real Clips

If you have actual CCTV footage, place it under `clips/` using this naming convention:
```
clips/
  STORE_BLR_001_CAM_ENTRY_01_20260303T100000.mp4
  STORE_BLR_001_CAM_FLOOR_01_20260303T100000.mp4
  STORE_BLR_001_CAM_BILLING_01_20260303T100000.mp4
```

Then run:
```bash
pip install ultralytics opencv-python-headless requests
cd pipeline
./run.sh --clips ../clips --api http://localhost:8000
```

Events will be written to `pipeline/events.jsonl` and simultaneously POSTed to the API.

**No clips?** The pipeline automatically falls back to the simulator:
```bash
cd pipeline
python simulator.py events.jsonl  # generates ~500 synthetic events
```

Then feed them into the running API:
```bash
python -c "
import json, requests
events = [json.loads(l) for l in open('events.jsonl') if l.strip()]
for i in range(0, len(events), 500):
    r = requests.post('http://localhost:8000/events/ingest', json=events[i:i+500])
    print(r.json())
"
```

---

## Architecture

```
pipeline/
  detect.py      YOLOv8n + ByteTrack + ReID → StoreEvent objects
  tracker.py     Per-store visitor session management + re-entry detection
  emit.py        Event schema, file/API emission
  simulator.py   Synthetic event generation (no footage required)
  run.sh         One-command clip processing

app/
  main.py        FastAPI entrypoint + middleware
  models.py      Pydantic schemas + SQLAlchemy ORM
  ingestion.py   Batch ingest, idempotent by event_id
  metrics.py     Real-time KPIs (visitors, conversion, dwell, queue)
  funnel.py      Entry → Zone → Billing → Purchase funnel
  heatmap.py     Zone visit frequency + dwell, normalised 0-100
  anomalies.py   Queue spike, conversion drop, dead zone, stale feed
  health.py      Per-store feed freshness check

dashboard/
  index.html     Live web dashboard (polls API every 5s)
```

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/events/ingest` | Batch ingest (max 500). Idempotent by event_id. |
| GET | `/stores/{id}/metrics` | Unique visitors, conversion rate, queue depth, abandonment |
| GET | `/stores/{id}/funnel` | Entry → Zone → Billing → Purchase with drop-off % |
| GET | `/stores/{id}/heatmap` | Zone visit frequency + dwell, score 0-100 |
| GET | `/stores/{id}/anomalies` | Queue spike, conversion drop, dead zone, stale feed |
| GET | `/health` | Per-store feed freshness; STALE_FEED if >10min lag |

Interactive docs: `http://localhost:8000/docs`

## Running Tests

```bash
pip install -r requirements.txt
pytest tests/ -v --cov=app --cov-report=term-missing
```

Coverage target: >70%. Edge cases covered: empty store, all-staff clip, re-entry in funnel, billing queue abandon, batch size limit, idempotent double-ingest.

## Design Decisions

See `docs/DESIGN.md` and `docs/CHOICES.md` for architecture rationale and AI-assisted decision log.
