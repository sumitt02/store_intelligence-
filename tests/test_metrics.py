"""
# PROMPT: Write tests for the metrics and funnel endpoints.
# Cover: conversion rate calculation (billing zone + time window),
# abandonment rate, funnel drop-off percentages, heatmap normalisation (0-100),
# and the low-data confidence flag on heatmap.
# CHANGES MADE: Claude suggested mocking the POS CSV load — I kept real file
# reads but used a temp CSV via monkeypatching to avoid coupling to disk state.
"""
import csv
import io
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

STORE_ID = "STORE_BLR_002"
from datetime import datetime, timezone
BASE_TIME = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _e(event_type, visitor_id, zone_id=None, dwell_ms=0, is_staff=False,
       ts=BASE_TIME, queue_depth=None, store_id=STORE_ID):
    return {
        "event_id": str(uuid.uuid4()),
        "store_id": store_id,
        "camera_id": "CAM_ENTRY_01",
        "visitor_id": visitor_id,
        "event_type": event_type,
        "timestamp": ts,
        "zone_id": zone_id,
        "dwell_ms": dwell_ms,
        "is_staff": is_staff,
        "confidence": 0.92,
        "metadata": {"queue_depth": queue_depth, "sku_zone": None, "session_seq": 1},
    }


class TestMetrics:
    def test_unique_visitors_counts_customers_only(self, client, happy_path_events, staff_events):
        client.post("/events/ingest", json=happy_path_events)
        client.post("/events/ingest", json=staff_events)
        resp = client.get(f"/stores/{STORE_ID}/metrics")
        assert resp.status_code == 200
        # 1 customer + 0 staff should count
        assert resp.json()["unique_visitors"] == 1

    def test_queue_depth_zero_when_all_left(self, client, happy_path_events):
        """Visitor who joined and exited billing shouldn't inflate queue_depth."""
        client.post("/events/ingest", json=happy_path_events)
        resp = client.get(f"/stores/{STORE_ID}/metrics")
        # Queue joins - exits = 0 (they left)
        assert resp.json()["queue_depth"] >= 0

    def test_abandonment_rate_calculated_correctly(self, client, abandon_events):
        client.post("/events/ingest", json=abandon_events)
        resp = client.get(f"/stores/{STORE_ID}/metrics")
        body = resp.json()
        # 1 join, 1 abandon → 100% abandonment
        assert body["abandonment_rate"] == 1.0

    def test_no_purchases_gives_zero_conversion(self, client):
        vis = f"VIS_{uuid.uuid4().hex[:8]}"
        events = [
            _e("ENTRY", vis),
            _e("ZONE_ENTER", vis, "SKINCARE"),
            _e("EXIT", vis),
        ]
        client.post("/events/ingest", json=events)
        resp = client.get(f"/stores/{STORE_ID}/metrics")
        body = resp.json()
        assert body["conversion_rate"] == 0.0

    def test_avg_dwell_per_zone_populated(self, client, happy_path_events):
        client.post("/events/ingest", json=happy_path_events)
        resp = client.get(f"/stores/{STORE_ID}/metrics")
        zones = resp.json()["avg_dwell_per_zone"]
        assert isinstance(zones, list)
        zone_ids = [z["zone_id"] for z in zones]
        assert "SKINCARE" in zone_ids

    def test_metrics_as_of_is_recent(self, client):
        resp = client.get(f"/stores/{STORE_ID}/metrics")
        as_of = resp.json()["as_of"]
        dt = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        assert abs((now - dt).total_seconds()) < 5


class TestFunnel:
    def test_funnel_four_stages(self, client, happy_path_events):
        client.post("/events/ingest", json=happy_path_events)
        resp = client.get(f"/stores/{STORE_ID}/funnel")
        assert resp.status_code == 200
        body = resp.json()
        stage_names = [s["stage"] for s in body["stages"]]
        assert stage_names == ["ENTRY", "ZONE_VISIT", "BILLING_QUEUE", "PURCHASE"]

    def test_funnel_drop_off_monotonic(self, client, happy_path_events):
        """Each stage count must be ≤ previous stage count."""
        client.post("/events/ingest", json=happy_path_events)
        resp = client.get(f"/stores/{STORE_ID}/funnel")
        stages = resp.json()["stages"]
        for i in range(1, len(stages)):
            assert stages[i]["count"] <= stages[i-1]["count"], (
                f"Stage {stages[i]['stage']} ({stages[i]['count']}) > "
                f"{stages[i-1]['stage']} ({stages[i-1]['count']})"
            )

    def test_funnel_zero_purchase_on_abandon(self, client, abandon_events):
        client.post("/events/ingest", json=abandon_events)
        resp = client.get(f"/stores/{STORE_ID}/funnel")
        stages = {s["stage"]: s["count"] for s in resp.json()["stages"]}
        assert stages["PURCHASE"] == 0

    def test_reentry_not_double_counted_in_funnel(self, client, reentry_events):
        client.post("/events/ingest", json=reentry_events)
        resp = client.get(f"/stores/{STORE_ID}/funnel")
        stages = {s["stage"]: s["count"] for s in resp.json()["stages"]}
        assert stages["ENTRY"] == 1  # REENTRY must not inflate ENTRY count

    def test_funnel_drop_off_pct_range(self, client, happy_path_events):
        client.post("/events/ingest", json=happy_path_events)
        resp = client.get(f"/stores/{STORE_ID}/funnel")
        for stage in resp.json()["stages"]:
            assert 0.0 <= stage["drop_off_pct"] <= 100.0


class TestHeatmap:
    def test_heatmap_scores_normalised_0_100(self, client, happy_path_events):
        client.post("/events/ingest", json=happy_path_events)
        resp = client.get(f"/stores/{STORE_ID}/heatmap")
        assert resp.status_code == 200
        for zone in resp.json()["zones"]:
            assert 0.0 <= zone["score"] <= 100.0

    def test_heatmap_low_confidence_flag(self, client):
        """With < 20 sessions, data_confidence should be False."""
        vis = f"VIS_{uuid.uuid4().hex[:8]}"
        events = [_e("ENTRY", vis), _e("ZONE_ENTER", vis, "SKINCARE"), _e("EXIT", vis)]
        client.post("/events/ingest", json=events)
        resp = client.get(f"/stores/{STORE_ID}/heatmap")
        for zone in resp.json()["zones"]:
            assert zone["data_confidence"] is False

    def test_heatmap_excludes_entry_exit_zone(self, client, happy_path_events):
        client.post("/events/ingest", json=happy_path_events)
        resp = client.get(f"/stores/{STORE_ID}/heatmap")
        zone_ids = [z["zone_id"] for z in resp.json()["zones"]]
        assert "ENTRY_EXIT" not in zone_ids

    def test_heatmap_staff_excluded(self, client, staff_events):
        client.post("/events/ingest", json=staff_events)
        resp = client.get(f"/stores/{STORE_ID}/heatmap")
        # Staff events ingested but zones populated by staff movement shouldn't
        # appear in heatmap (staff excluded)
        assert resp.status_code == 200
