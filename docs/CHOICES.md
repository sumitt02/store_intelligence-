# Architecture Choices

Three decisions, each with options considered, what AI suggested, and what I chose and why.

---

## Decision 1: Detection Model

**Question**: Which person detection model to use for 1080p/15fps CCTV footage?

### Options Considered

| Model | Pros | Cons |
|-------|------|------|
| YOLOv8n | Fast on CPU, well-maintained, Ultralytics ByteTrack integration built-in | Slightly lower accuracy than larger models |
| YOLOv8x | Higher mAP | Too slow for real-time on CPU (8+ sec/frame) |
| RT-DETR | Transformer-based, better occlusion handling | Requires GPU; complex setup |
| MediaPipe | Very fast, edge-optimised | Limited to close-range detection; poor at CCTV angles |

### What AI Suggested

Claude suggested starting with YOLOv8n and noted that the ByteTrack integration in Ultralytics (`model.track(tracker="bytetrack.yaml")`) is the lowest-friction path to stable tracking IDs. Claude also suggested trying RT-DETR for the billing camera specifically (crowded scene) but noted it would complicate the deployment.

### What I Chose

**YOLOv8n + ByteTrack**, uniform across all cameras.

**Why**: The problem explicitly says "production-aware" — a model that requires GPU is not deployable in most retail edge environments. YOLOv8n runs at ~15fps on CPU for 1080p (matching the clip FPS). Mixing RT-DETR for one camera would complicate the pipeline significantly for marginal accuracy gain.

**If I were doing this for a real production system**: I would run YOLOv8x on a T4 GPU instance per store cluster, and add a dedicated crowded-scene model (like CrowdDet) for the billing camera specifically.

**VLM usage**: I used Claude Vision to evaluate detection quality on a sample frame — specifically to check if staff (in uniform) could be distinguished from customers via upper-body crop analysis. The VLM correctly identified colour difference but this was too slow (800ms/frame) for real-time use. I kept the trajectory-based heuristic (3+ zones in <2 min) which runs in microseconds.

---

## Decision 2: Event Schema Design

**Question**: How should the event schema handle zone-specific fields (queue_depth, sku_zone) that only apply to certain event types?

### Options Considered

**Option A: Flat schema with nullable fields**
```json
{"event_type": "ENTRY", "zone_id": null, "queue_depth": null, "sku_zone": null}
```
Pro: Simple. Con: Every event carries fields that are meaningless for its type.

**Option B: Discriminated union per event type**
```json
{"event_type": "BILLING_QUEUE_JOIN", "billing_payload": {"queue_depth": 3}}
```
Pro: Type-safe. Con: Complex to validate; harder to query with SQL WHERE clauses.

**Option C: Flat base + metadata object for optional fields** (chosen)
```json
{"event_type": "ENTRY", "zone_id": null, "metadata": {"queue_depth": null, "sku_zone": null, "session_seq": 1}}
```
Pro: Clean separation; metadata is extensible without schema migration; SQL queries on base fields remain simple.

### What AI Suggested

Claude suggested Option B (discriminated union) arguing it gives compile-time guarantees. I disagreed: the scoring harness expects a flat-ish schema (the problem statement shows a single JSON object), and the SQL query complexity of querying across union types is significant.

### What I Chose

**Option C** — flat base with metadata object.

**Why I overrode the AI**: The problem statement provides an explicit schema showing `metadata` as a nested object. Option B would have made the `POST /events/ingest` validation logic significantly more complex and wouldn't match the example schema. The `session_seq` field inside metadata was my addition — it enables ordering events within a session without relying on timestamps (useful when events arrive out-of-order from multiple cameras).

---

## Decision 3: API Storage Engine

**Question**: SQLite or PostgreSQL for the Intelligence API?

### Options Considered

| Option | Pros | Cons |
|--------|------|------|
| SQLite (WAL mode) | Zero infra, single `docker compose up`, no connection pool needed | Not horizontally scalable; max ~1000 concurrent writes/sec |
| PostgreSQL | Battle-tested at scale, full SQL, TimescaleDB extension for time-series | Requires separate container, connection pool, migration tooling |
| Redis | Sub-millisecond reads, good for counters | No relational queries; would need a separate persistence layer |

### What AI Suggested

Claude's first response suggested PostgreSQL with asyncpg and a connection pool, arguing it's "production-aware." When I pointed out the acceptance gate requirement ("docker compose up starts everything, no manual steps beyond git clone"), Claude acknowledged that PostgreSQL is correct for a real production system but SQLite + WAL mode is the right call for this submission.

### What I Chose

**SQLite with WAL mode + PRAGMA optimisations**.

Configuration applied at startup:
```sql
PRAGMA journal_mode=WAL;    -- concurrent reads don't block writes
PRAGMA synchronous=NORMAL;  -- safe + faster than FULL
PRAGMA cache_size=10000;    -- 10MB page cache
```

**Why**: The submission must work with `docker compose up` on a clean machine. A PostgreSQL dependency adds ~2 minutes of init time and requires the reviewer to have Docker with enough memory. SQLite handles this problem's scale (40 stores, ~5 events/sec per store) with room to spare.

**Scale breakpoint**: If average event rate exceeds ~500/sec sustained, I'd migrate to PostgreSQL + TimescaleDB with a hypertable on `(store_id, timestamp)`. The SQLAlchemy ORM layer makes this migration straightforward — only `DATABASE_URL` and the `PRAGMA` block change.

---

## Decision 4: Real CCTV Camera Zone Calibration (Brigade Bangalore)

**Question**: How should zone boundaries be determined from actual camera footage — manually annotated, auto-detected, or heuristic?

### Options Considered

| Approach | Pros | Cons |
|----------|------|------|
| Manual polygon annotation (CVAT/LabelStudio) | Ground-truth accuracy | Hours of annotation per camera; requires annotators |
| Homography from floor plan | Accurate zone mapping | Needs camera calibration matrix; complex setup |
| Equal horizontal slices (chosen) | Zero setup; works for linear shelf arrangements | Imprecise for non-linear zones |
| ML-based zone segmentation | Best accuracy | Requires training data for this store |

### What AI Suggested

Claude first suggested building a homography-based zone mapper — projecting camera pixels onto the store floor plan using 4-point correspondence. This would be the most accurate approach.

### What I Chose

**Equal horizontal slices for shelf cameras (CAM_01, CAM_02); x-threshold for billing (CAM_05); y-threshold for entry (CAM_03).**

**Why I overrode the AI**: Brigade Road's shelving is a linear back-wall arrangement (confirmed by visual inspection of real frames). Equal x-slices (n_zones wide strips) correctly map to the brand sections arranged left-to-right on each shelf. For CAM_05, visual inspection confirmed the billing laptop is strictly to the left (~45%) and accessories to the right — a single x=0.45 threshold is sufficient.

The homography approach would require: (a) identifying 4 known ground-plane points per camera, (b) measuring their real-world coordinates from the store layout PDF, (c) computing the perspective transform matrix. Given the footage is only 2.3 minutes per camera, the cost-benefit didn't justify the complexity.

**If scaling to all stores**: I would build a one-time calibration UI where a store manager clicks 4 floor markers per camera, automatically computing the homography matrix and storing it in `store_layout.json` under `cameras[cam_id].homography`.

---

## Decision 5: Date-Parameterised Metrics API

**Question**: How should the API handle historical data from the real Brigade footage (April 10) vs. live data (today)?

### What AI Suggested

Claude initially built metrics with `WHERE timestamp >= today` filtering. When we ingested real April footage, all metrics returned 0.

### What I Chose

Added an optional `?date=YYYY-MM-DD` query parameter to `/metrics`, `/funnel`, and `/heatmap`. When provided, the date window shifts to that day's UTC midnight → midnight. Defaults to today for live operation.

**Why**: This is the correct production design — dashboards need historical replay for audits, shift reports, and forensics. The alternative (always using today) would make historical data invisible. The date param adds 3 lines per endpoint and zero complexity to callers.
