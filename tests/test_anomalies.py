"""
# PROMPT: Write tests for anomaly detection: queue spike, conversion drop,
# dead zone, and stale feed. Each should test trigger condition AND
# that the anomaly is NOT triggered when below threshold.
# CHANGES MADE: Claude suggested a single parametrize for all anomaly types.
# I split into separate classes for readability and because the setup
# for each type is fundamentally different (different event patterns).
"""
import uuid
from datetime import datetime, timezone, timedelta

import pytest

STORE_ID = "STORE_BLR_002"


def _ts(delta_minutes: float = 0) -> str:
    # Use current time as base so events are always within 5-min anomaly window
    base = datetime.now(timezone.utc)
    return (base + timedelta(minutes=delta_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _e(event_type, visitor_id, zone_id=None, is_staff=False,
       dwell_ms=0, ts=None, queue_depth=None, store_id=STORE_ID):
    return {
        "event_id": str(uuid.uuid4()),
        "store_id": store_id,
        "camera_id": "CAM_BILLING_01",
        "visitor_id": visitor_id,
        "event_type": event_type,
        "timestamp": ts or _ts(),
        "zone_id": zone_id,
        "dwell_ms": dwell_ms,
        "is_staff": is_staff,
        "confidence": 0.9,
        "metadata": {"queue_depth": queue_depth, "sku_zone": None, "session_seq": 1},
    }


class TestQueueSpikeAnomaly:
    def test_queue_spike_detected_above_threshold(self, client):
        """6 visitors joining billing queue in 5min → BILLING_QUEUE_SPIKE."""
        events = []
        for _ in range(6):
            vis = f"VIS_{uuid.uuid4().hex[:8]}"
            events.append(_e("BILLING_QUEUE_JOIN", vis, "BILLING", queue_depth=6,
                             ts=_ts(0)))
        client.post("/events/ingest", json=events)

        resp = client.get(f"/stores/{STORE_ID}/anomalies")
        assert resp.status_code == 200
        anomaly_types = [a["anomaly_type"] for a in resp.json()["anomalies"]]
        assert "BILLING_QUEUE_SPIKE" in anomaly_types

    def test_no_spike_below_threshold(self, client):
        """2 visitors in queue — below threshold, no anomaly."""
        events = []
        for _ in range(2):
            vis = f"VIS_{uuid.uuid4().hex[:8]}"
            events.append(_e("BILLING_QUEUE_JOIN", vis, "BILLING", queue_depth=2, ts=_ts(0)))
            events.append(_e("EXIT", vis, ts=_ts(5)))
        client.post("/events/ingest", json=events)

        resp = client.get(f"/stores/{STORE_ID}/anomalies")
        anomaly_types = [a["anomaly_type"] for a in resp.json()["anomalies"]]
        assert "BILLING_QUEUE_SPIKE" not in anomaly_types

    def test_queue_spike_has_severity(self, client):
        events = []
        for _ in range(12):
            vis = f"VIS_{uuid.uuid4().hex[:8]}"
            events.append(_e("BILLING_QUEUE_JOIN", vis, "BILLING", queue_depth=12, ts=_ts(0)))
        client.post("/events/ingest", json=events)

        resp = client.get(f"/stores/{STORE_ID}/anomalies")
        spike = next(
            (a for a in resp.json()["anomalies"] if a["anomaly_type"] == "BILLING_QUEUE_SPIKE"),
            None,
        )
        assert spike is not None
        assert spike["severity"] in ("WARN", "CRITICAL")
        assert "suggested_action" in spike
        assert len(spike["suggested_action"]) > 0


class TestStaleFeedException:
    def test_stale_feed_old_events(self, client):
        """Events from 30 min ago → STALE_FEED anomaly."""
        vis = f"VIS_{uuid.uuid4().hex[:8]}"
        old_ts = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        events = [_e("ENTRY", vis, ts=old_ts)]
        client.post("/events/ingest", json=events)

        resp = client.get(f"/stores/{STORE_ID}/anomalies")
        anomaly_types = [a["anomaly_type"] for a in resp.json()["anomalies"]]
        assert "STALE_FEED" in anomaly_types

    def test_no_stale_feed_with_recent_events(self, client, happy_path_events):
        """Recent events → no STALE_FEED."""
        # Inject recent events with near-current timestamps
        now = datetime.now(timezone.utc)
        for e in happy_path_events:
            e["timestamp"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            e["event_id"] = str(uuid.uuid4())  # fresh IDs
        client.post("/events/ingest", json=happy_path_events)

        resp = client.get(f"/stores/{STORE_ID}/anomalies")
        anomaly_types = [a["anomaly_type"] for a in resp.json()["anomalies"]]
        assert "STALE_FEED" not in anomaly_types


class TestAnomalyResponseSchema:
    def test_anomalies_response_schema(self, client):
        resp = client.get(f"/stores/{STORE_ID}/anomalies")
        assert resp.status_code == 200
        body = resp.json()
        assert "store_id" in body
        assert "as_of" in body
        assert "anomalies" in body
        for anomaly in body["anomalies"]:
            assert "anomaly_id" in anomaly
            assert "anomaly_type" in anomaly
            assert "severity" in anomaly
            assert anomaly["severity"] in ("INFO", "WARN", "CRITICAL")
            assert "suggested_action" in anomaly
            assert "detected_at" in anomaly

    def test_empty_store_anomalies_no_crash(self, client):
        resp = client.get("/stores/STORE_DEL_001/anomalies")
        assert resp.status_code == 200
        assert resp.json()["anomalies"] == []


class TestHealthEndpoint:
    def test_health_returns_store_statuses(self, client, happy_path_events):
        now = datetime.now(timezone.utc)
        for e in happy_path_events:
            e["timestamp"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            e["event_id"] = str(uuid.uuid4())
        client.post("/events/ingest", json=happy_path_events)

        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] in ("ok", "degraded")
        assert isinstance(body["stores"], list)
        for store in body["stores"]:
            assert "store_id" in store
            assert "status" in store
            assert store["status"] in ("OK", "STALE_FEED", "NO_DATA")

    def test_health_stale_store_marked_degraded(self, client):
        vis = f"VIS_{uuid.uuid4().hex[:8]}"
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        client.post("/events/ingest", json=[_e("ENTRY", vis, ts=old_ts)])

        resp = client.get("/health")
        body = resp.json()
        stale = [s for s in body["stores"] if s["status"] == "STALE_FEED"]
        assert len(stale) >= 1
        assert body["status"] == "degraded"
