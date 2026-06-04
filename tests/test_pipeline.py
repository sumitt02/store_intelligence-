"""
# PROMPT: Write tests for the event ingestion pipeline covering:
# schema validation, idempotency, partial success, group entry counting,
# staff exclusion, re-entry handling, and empty payloads.
# CHANGES MADE: Added explicit tests for zero-event batches and
# the 500-event batch limit (Claude's suggestion was to only test 1 and 500,
# I added 501 as the boundary case to verify the reject behaviour).
"""
import uuid
import pytest


STORE_ID = "STORE_BLR_002"


def _make_event(event_type="ENTRY", zone_id=None, is_staff=False,
                visitor_id=None, store_id=STORE_ID, confidence=0.9,
                timestamp="2026-03-03T10:00:00Z", dwell_ms=0) -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "store_id": store_id,
        "camera_id": "CAM_ENTRY_01",
        "visitor_id": visitor_id or f"VIS_{uuid.uuid4().hex[:8]}",
        "event_type": event_type,
        "timestamp": timestamp,
        "zone_id": zone_id,
        "dwell_ms": dwell_ms,
        "is_staff": is_staff,
        "confidence": confidence,
        "metadata": {"queue_depth": None, "sku_zone": None, "session_seq": 1},
    }


class TestIngest:
    def test_happy_path_ingest(self, client, happy_path_events):
        resp = client.post("/events/ingest", json=happy_path_events)
        assert resp.status_code == 200
        body = resp.json()
        assert body["accepted"] == len(happy_path_events)
        assert body["duplicates"] == 0
        assert body["rejected"] == 0

    def test_idempotent_double_ingest(self, client, happy_path_events):
        """Same payload ingested twice — second call must return all duplicates."""
        r1 = client.post("/events/ingest", json=happy_path_events)
        r2 = client.post("/events/ingest", json=happy_path_events)
        assert r1.status_code == 200
        assert r2.status_code == 200
        b1, b2 = r1.json(), r2.json()
        assert b1["accepted"] == len(happy_path_events)
        assert b2["duplicates"] == len(happy_path_events)
        assert b2["accepted"] == 0

    def test_partial_success_malformed_event(self, client):
        """One bad event in a batch — valid ones still accepted."""
        good = _make_event("ENTRY")
        bad = {"event_id": "not-a-uuid", "event_type": "INVALID_TYPE"}
        resp = client.post("/events/ingest", json=[good, bad])
        assert resp.status_code == 200
        body = resp.json()
        assert body["accepted"] == 1
        assert body["rejected"] == 1
        assert len(body["errors"]) == 1

    def test_empty_batch(self, client):
        resp = client.post("/events/ingest", json=[])
        assert resp.status_code == 200
        body = resp.json()
        assert body["accepted"] == 0
        assert body["duplicates"] == 0
        assert body["rejected"] == 0

    def test_batch_too_large(self, client):
        events = [_make_event() for _ in range(501)]
        resp = client.post("/events/ingest", json=events)
        assert resp.status_code == 413

    def test_non_array_body(self, client):
        resp = client.post("/events/ingest", json={"event_type": "ENTRY"})
        assert resp.status_code == 422

    def test_invalid_event_type(self, client):
        evt = _make_event()
        evt["event_type"] = "RANDOM_GARBAGE"
        resp = client.post("/events/ingest", json=[evt])
        assert resp.status_code == 200
        assert resp.json()["rejected"] == 1

    def test_zone_required_for_zone_enter(self, client):
        """ZONE_ENTER without zone_id must be rejected."""
        evt = _make_event("ZONE_ENTER", zone_id=None)
        resp = client.post("/events/ingest", json=[evt])
        assert resp.status_code == 200
        assert resp.json()["rejected"] == 1

    def test_confidence_out_of_range(self, client):
        evt = _make_event(confidence=1.5)
        resp = client.post("/events/ingest", json=[evt])
        assert resp.status_code == 200
        assert resp.json()["rejected"] == 1

    def test_invalid_timestamp_format(self, client):
        evt = _make_event(timestamp="March 3rd 2026")
        resp = client.post("/events/ingest", json=[evt])
        assert resp.status_code == 200
        assert resp.json()["rejected"] == 1


class TestStaffExclusion:
    def test_staff_events_accepted_but_excluded_from_metrics(self, client, staff_events):
        resp = client.post("/events/ingest", json=staff_events)
        assert resp.status_code == 200
        assert resp.json()["accepted"] == len(staff_events)

        metrics_resp = client.get(f"/stores/{STORE_ID}/metrics")
        assert metrics_resp.status_code == 200
        # Staff ENTRY events should not inflate unique_visitors
        metrics = metrics_resp.json()
        assert metrics["unique_visitors"] == 0  # only staff events, no customer ENTRY


class TestGroupEntry:
    def test_group_entry_counts_individuals(self, client, group_entry_events):
        """3 simultaneous entries must produce unique_visitors=3, not 1."""
        resp = client.post("/events/ingest", json=group_entry_events)
        assert resp.status_code == 200
        assert resp.json()["accepted"] == 3

        metrics_resp = client.get(f"/stores/{STORE_ID}/metrics")
        metrics = metrics_resp.json()
        assert metrics["unique_visitors"] == 3


class TestReentry:
    def test_reentry_event_accepted(self, client, reentry_events):
        resp = client.post("/events/ingest", json=reentry_events)
        assert resp.status_code == 200
        body = resp.json()
        assert body["accepted"] == len(reentry_events)
        assert body["rejected"] == 0

    def test_reentry_does_not_double_count_in_funnel(self, client, reentry_events):
        """Funnel ENTRY stage should count unique visitors, not REENTRY events."""
        client.post("/events/ingest", json=reentry_events)
        funnel_resp = client.get(f"/stores/{STORE_ID}/funnel")
        assert funnel_resp.status_code == 200
        stages = {s["stage"]: s["count"] for s in funnel_resp.json()["stages"]}
        # 1 unique visitor (ENTRY only, not REENTRY)
        assert stages["ENTRY"] == 1


class TestEmptyStore:
    def test_metrics_empty_store_no_crash(self, client):
        """Zero-traffic store must return zeros, not crash or return null."""
        resp = client.get("/stores/STORE_DEL_001/metrics")
        assert resp.status_code == 200
        body = resp.json()
        assert body["unique_visitors"] == 0
        assert body["conversion_rate"] == 0.0
        assert body["queue_depth"] == 0

    def test_funnel_empty_store(self, client):
        resp = client.get("/stores/STORE_DEL_001/funnel")
        assert resp.status_code == 200
        for stage in resp.json()["stages"]:
            assert stage["count"] == 0

    def test_heatmap_empty_store(self, client):
        resp = client.get("/stores/STORE_DEL_001/heatmap")
        assert resp.status_code == 200
        assert resp.json()["zones"] == []


class TestSchemaCompliance:
    def test_event_ids_are_unique_after_ingest(self, client):
        events = [_make_event() for _ in range(10)]
        event_ids = [e["event_id"] for e in events]
        assert len(event_ids) == len(set(event_ids))
        resp = client.post("/events/ingest", json=events)
        assert resp.json()["accepted"] == 10

    def test_metrics_response_has_required_fields(self, client, happy_path_events):
        client.post("/events/ingest", json=happy_path_events)
        resp = client.get(f"/stores/{STORE_ID}/metrics")
        body = resp.json()
        for field in ["store_id", "as_of", "unique_visitors", "conversion_rate",
                      "avg_dwell_per_zone", "queue_depth", "abandonment_rate"]:
            assert field in body, f"Missing field: {field}"

    def test_health_endpoint(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert "status" in body
        assert "checked_at" in body
        assert "stores" in body
