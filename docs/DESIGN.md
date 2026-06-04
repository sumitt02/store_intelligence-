# System Design — Store Intelligence Platform

## Overview

A complete pipeline converting raw CCTV footage into real-time retail analytics. The system processes video from up to 40 physical stores, emits structured behavioural events, and exposes a queryable REST API with live dashboard.

```
CCTV Clips → Detection Layer → Event Stream → Intelligence API → Live Dashboard
               (YOLOv8 +           (JSONL /         (FastAPI +        (nginx +
               ByteTrack +          HTTP POST)        SQLite)           HTML/JS)
               ReID Tracker)
```

---

## Stage 1: Detection Layer

### Model Stack

| Component | Choice | Rationale |
|-----------|--------|-----------|
| Object detection | YOLOv8n | Best speed/accuracy tradeoff for real-time 1080p/15fps. Runs on CPU without GPU — important for retail edge deployment. |
| Multi-object tracking | ByteTrack | Two-stage association (high-conf + low-conf detections) handles partial occlusion better than SORT/DeepSORT. |
| Re-ID | Bounding box trajectory + running embedding average | OSNet was evaluated but is too slow for CPU-only deployment at this store count. |

### Zone Classification

Zone boundaries are derived from `store_layout.json`. Each camera's coverage list (e.g., `["SKINCARE", "HAIRCARE"]`) is divided into equal horizontal slices of the frame. This is a practical approximation — production would use per-camera calibration polygons.

### Staff Detection

Two signals combined:
1. **Trajectory speed**: persons visiting 3+ distinct zones in under 2 minutes → classified as staff
2. **Uniform colour** (future): upper-body RGB histogram vs. known staff uniform palette

Staff events are ingested with `is_staff=true` and excluded from all customer-facing metrics.

### Edge Case Handling

| Edge Case | Approach |
|-----------|----------|
| Group entry | Each YOLO detection box is independent — ByteTrack assigns separate track IDs. 3 people entering → 3 ENTRY events. |
| Re-entry | ReIDTracker maintains a 5-minute exit buffer. Returning visitor_id within window → REENTRY not new ENTRY. |
| Partial occlusion | ByteTrack low-confidence association re-links partially occluded tracks. Confidence is preserved in emitted event. |
| Camera overlap | Floor + entry camera coverage defined non-overlapping in store_layout.json. Cross-camera dedup via shared visitor_id namespace per store. |
| Empty store | Detection loop runs normally, emits no events. API handles zero-visitor state without crashing. |

---

## Stage 2: Event Stream

### Schema Design

Events follow a flat structure with a nested `metadata` object for type-specific fields (queue_depth, sku_zone). Key decisions:

- **`is_staff` on every event**: not filtered at emission — the API layer decides what to count. This preserves full audit trail.
- **`confidence` never suppressed**: low-confidence detections are emitted with their real confidence score. The API and dashboard show this signal rather than silently dropping events.
- **`session_seq`**: ordinal position within a visitor's session. Enables replay ordering and session reconstruction without relying on timestamp ordering (network jitter).
- **`dwell_ms` on ZONE_EXIT**: computed from frame delta at exit time, not approximated. For ZONE_DWELL (30s ticks) it represents cumulative dwell.

### Ingest Pipeline

Events flow: `detect.py` → `emit.py` → `POST /events/ingest`. The pipeline can also write JSONL to disk for offline replay (`run.sh` handles both paths).

---

## Stage 3: Intelligence API

### Storage: SQLite

Chosen over PostgreSQL because:
- Zero additional infrastructure (no separate container to health-check)
- WAL mode gives concurrent read/write without blocking
- Sufficient for 40 stores × 8 cameras × 20 min clips = ~200K events/day
- Documented trade-off: switch to PostgreSQL if events/sec exceeds ~1000 sustained

### Endpoint Architecture

All metric endpoints query raw events at request time — no pre-aggregation cache. This means:
- Metrics are always current (no staleness from a cache)
- Slower at high event volume — acceptable for the 40-store scale

For 400+ stores, I would add a background aggregation job that materialises hourly summaries, and serve cached results with a `stale_before` timestamp.

### Conversion Rate Calculation

POS transactions have no customer_id. Correlation: visitor present in BILLING zone in the 5-minute window before transaction timestamp, same store. This deliberately over-counts slightly (two visitors checking out simultaneously both count) — production would need POS terminal proximity data to improve accuracy.

### Idempotency

`POST /events/ingest` uses SQLite's `INSERT OR IGNORE` on `event_id` (primary key). Calling the endpoint twice with the same payload returns `accepted=0, duplicates=N` with HTTP 200. No partial state possible.

---

## Stage 4: Live Dashboard

Single-page HTML/JS polling the API every 5 seconds. No build toolchain — serves directly from nginx. Shows:
- KPI cards (visitors, conversion, queue, abandonment)
- Live funnel with animated bars
- Zone heatmap (score = 60% visit frequency + 40% avg dwell, normalised 0-100)
- Active anomaly feed with severity and suggested actions
- Simulated live event log

---

## AI-Assisted Decisions

### 1. ByteTrack over DeepSORT
I asked Claude to compare ByteTrack, DeepSORT, and StrongSORT for the partial occlusion edge case (people partially behind product displays). Claude highlighted ByteTrack's two-stage association as specifically designed for low-confidence detections from occluded objects. I evaluated this against the problem statement's explicit mention of "partial occlusion" as a known challenge and agreed. **Agreed with AI recommendation.**

### 2. SQLite vs PostgreSQL
Claude initially suggested PostgreSQL with a connection pool. I pushed back: this is a challenge submission requiring `docker compose up` with no additional setup. Claude acknowledged that SQLite + WAL mode is the correct choice here and would only be a bottleneck at >1000 events/sec sustained throughput. **Overrode AI recommendation; kept SQLite.**

### 3. Confidence in emitted events
Claude's first draft of `detect.py` suppressed detections below 0.4 confidence entirely. The problem statement explicitly says "do not suppress low-conf events" and "confidence calibration" is a scoring criterion. I corrected this — low-confidence events are emitted with their real confidence score. **Identified error in AI output; corrected it.**

---

## Production Gaps (Known)

1. **No GPU support** in current Dockerfile — would need CUDA base image for real-time inference at 40 stores
2. **SQLite at scale** — switch to PostgreSQL + TimescaleDB for event time-series at high throughput
3. **No auth** on API endpoints — production needs API key or mTLS between pipeline and API
4. **Zone calibration** — current zone mapping uses equal horizontal slices; real deployment needs per-camera polygon calibration
