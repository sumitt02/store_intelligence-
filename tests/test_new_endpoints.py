"""Tests for revenue, journey, and insights endpoints (Part D additions)."""
from __future__ import annotations
import uuid
from datetime import datetime, timezone, timedelta

import pytest
from fastapi.testclient import TestClient

BASE_TIME = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
STORE = "STORE_NE_01"
CAM = "CAM_ENTRY_01"


def _ts(offset_s: int = 0) -> str:
    return (BASE_TIME + timedelta(seconds=offset_s)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ev(event_type, visitor_id, zone_id=None, dwell_ms=0, is_staff=False, offset_s=0, store=STORE):
    return {
        "event_id": str(uuid.uuid4()),
        "store_id": store,
        "camera_id": CAM,
        "visitor_id": visitor_id,
        "event_type": event_type,
        "timestamp": _ts(offset_s),
        "zone_id": zone_id,
        "dwell_ms": dwell_ms,
        "is_staff": is_staff,
        "confidence": 0.92,
        "metadata": {"queue_depth": None, "sku_zone": zone_id, "session_seq": 1},
    }


# ── Revenue ──────────────────────────────────────────────────────────────────

class TestRevenue:
    def test_empty_store_returns_zero(self, client):
        r = client.get("/stores/STORE_REV_EMPTY/revenue")
        assert r.status_code == 200
        d = r.json()
        assert d["total_revenue_inr"] == 0.0
        assert d["transaction_count"] == 0
        assert d["unique_visitors"] == 0

    def test_response_has_required_fields(self, client):
        r = client.get(f"/stores/{STORE}/revenue")
        assert r.status_code == 200
        d = r.json()
        for key in ("store_id", "date", "as_of", "total_revenue_inr", "transaction_count",
                    "avg_basket_value_inr", "unique_visitors", "revenue_per_visitor_inr",
                    "converted_visitors", "conversion_rate_pct", "hourly_timeline", "pre_purchase_zones"):
            assert key in d, f"Missing key: {key}"

    def test_date_param_filters_correctly(self, client):
        r = client.get(f"/stores/{STORE}/revenue?date=2025-01-15")
        assert r.status_code == 200
        assert r.json()["date"] == "2025-01-15"

    def test_visitors_counted_in_revenue(self, client):
        store = f"STORE_REV_{uuid.uuid4().hex[:6]}"
        events = [_ev("ENTRY", f"V{i}", offset_s=i*60, store=store) for i in range(5)]
        client.post("/events/ingest", json=events)
        r = client.get(f"/stores/{store}/revenue")
        assert r.json()["unique_visitors"] == 5

    def test_hourly_timeline_is_list(self, client):
        r = client.get(f"/stores/{STORE}/revenue")
        tl = r.json()["hourly_timeline"]
        assert isinstance(tl, list)
        if tl:
            assert "hour" in tl[0] and "revenue" in tl[0] and "transactions" in tl[0]


# ── Journey ───────────────────────────────────────────────────────────────────

class TestJourney:
    def test_empty_store_returns_zero_sessions(self, client):
        r = client.get("/stores/STORE_JRN_EMPTY/journey")
        assert r.status_code == 200
        d = r.json()
        assert d["unique_sessions_with_zone_visits"] == 0
        assert d["avg_zones_per_session"] == 0.0
        assert d["top_transitions"] == []
        assert d["brand_affinity"] == []

    def test_response_structure(self, client):
        r = client.get(f"/stores/{STORE}/journey")
        assert r.status_code == 200
        d = r.json()
        for key in ("store_id", "date", "unique_sessions_with_zone_visits",
                    "avg_zones_per_session", "journey_length_distribution",
                    "top_entry_zones", "top_exit_zones", "top_transitions", "brand_affinity"):
            assert key in d, f"Missing: {key}"

    def test_single_visitor_two_zones(self, client):
        store = f"STORE_JRN_{uuid.uuid4().hex[:6]}"
        events = [
            _ev("ENTRY", "V1", offset_s=0, store=store),
            _ev("ZONE_ENTER", "V1", "MAYBELLINE", offset_s=30, store=store),
            _ev("ZONE_ENTER", "V1", "LAKME_SKIN", offset_s=120, store=store),
        ]
        client.post("/events/ingest", json=events)
        r = client.get(f"/stores/{store}/journey")
        d = r.json()
        assert d["unique_sessions_with_zone_visits"] == 1
        assert d["avg_zones_per_session"] == 2.0
        assert "MAYBELLINE" in d["top_entry_zones"]

    def test_transition_counted_across_visitors(self, client):
        store = f"STORE_TR_{uuid.uuid4().hex[:6]}"
        events = []
        for i, visitor in enumerate(["V1", "V2", "V3"]):
            base = i * 400
            events += [
                _ev("ZONE_ENTER", visitor, "COLORBAR_SUGAR", offset_s=base, store=store),
                _ev("ZONE_ENTER", visitor, "SWISS_BEAUTY", offset_s=base+60, store=store),
            ]
        client.post("/events/ingest", json=events)
        r = client.get(f"/stores/{store}/journey")
        transitions = r.json()["top_transitions"]
        assert len(transitions) > 0
        assert transitions[0]["from"] == "COLORBAR_SUGAR"
        assert transitions[0]["to"] == "SWISS_BEAUTY"
        assert transitions[0]["count"] == 3

    def test_brand_affinity_co_visits(self, client):
        store = f"STORE_AFF_{uuid.uuid4().hex[:6]}"
        events = []
        for i, visitor in enumerate(["V1", "V2"]):
            base = i * 400
            events += [
                _ev("ZONE_ENTER", visitor, "AQUALOGICA", offset_s=base, store=store),
                _ev("ZONE_ENTER", visitor, "MINIMALIST", offset_s=base+60, store=store),
            ]
        client.post("/events/ingest", json=events)
        r = client.get(f"/stores/{store}/journey")
        affinity = r.json()["brand_affinity"]
        assert len(affinity) > 0
        top = affinity[0]
        zones = {top["zone_a"], top["zone_b"]}
        assert zones == {"AQUALOGICA", "MINIMALIST"}
        assert top["co_visits"] == 2

    def test_date_param_respected(self, client):
        r = client.get(f"/stores/{STORE}/journey?date=2025-03-01")
        assert r.status_code == 200
        assert r.json()["date"] == "2025-03-01"

    def test_exclude_zones_not_in_paths(self, client):
        """ENTRY_EXIT, BILLING, CASH_COUNTER, STOCKROOM should be excluded from path analysis."""
        store = f"STORE_EXC_{uuid.uuid4().hex[:6]}"
        events = [
            _ev("ZONE_ENTER", "V1", "ENTRY_EXIT", offset_s=0, store=store),
            _ev("ZONE_ENTER", "V1", "BILLING", offset_s=60, store=store),
            _ev("ZONE_ENTER", "V1", "CASH_COUNTER", offset_s=120, store=store),
            _ev("ZONE_ENTER", "V1", "MAYBELLINE", offset_s=180, store=store),
        ]
        client.post("/events/ingest", json=events)
        r = client.get(f"/stores/{store}/journey")
        d = r.json()
        # Only MAYBELLINE should appear in entry zones (others excluded)
        assert "ENTRY_EXIT" not in d["top_entry_zones"]
        assert "BILLING" not in d["top_entry_zones"]
        assert "MAYBELLINE" in d["top_entry_zones"]


# ── Insights ──────────────────────────────────────────────────────────────────

class TestInsights:
    def test_insights_structure(self, client):
        r = client.get(f"/stores/{STORE}/insights")
        assert r.status_code == 200
        d = r.json()
        assert "insights" in d
        assert "summary" in d
        assert "insight_count" in d
        assert isinstance(d["insights"], list)
        assert d["insight_count"] == len(d["insights"])
        for k in ("critical", "high", "medium", "low"):
            assert k in d["summary"]

    def test_insights_priority_values(self, client):
        r = client.get(f"/stores/{STORE}/insights")
        valid = {"CRITICAL", "HIGH", "MEDIUM", "LOW"}
        for ins in r.json()["insights"]:
            assert ins["priority"] in valid

    def test_insights_with_queue_abandonment(self, client):
        store = f"STORE_INS_{uuid.uuid4().hex[:6]}"
        events = []
        # 10 visitors entering
        for i in range(10):
            events.append(_ev("ENTRY", f"U{i}", offset_s=i*30, store=store))
        # 5 billing queue joins at CASH_COUNTER
        for i in range(5):
            events.append(_ev("BILLING_QUEUE_JOIN", f"U{i}", "CASH_COUNTER", offset_s=300+i*10, store=store))
        # 4 abandons (80% rate)
        for i in range(4):
            events.append(_ev("BILLING_QUEUE_ABANDON", f"U{i}", "CASH_COUNTER", offset_s=400+i*10, store=store))
        client.post("/events/ingest", json=events)
        r = client.get(f"/stores/{store}/insights")
        assert r.status_code == 200
        ids = [i["insight_id"] for i in r.json()["insights"]]
        # should have queue abandonment insight
        assert any("queue" in i or "abandon" in i for i in ids)

    def test_insights_date_param(self, client):
        r = client.get(f"/stores/{STORE}/insights?date=2025-04-01")
        assert r.status_code == 200
        assert r.json()["date"] == "2025-04-01"

    def test_insights_each_has_required_fields(self, client):
        store = f"STORE_INS2_{uuid.uuid4().hex[:6]}"
        # Create enough data to trigger insights
        events = []
        for i in range(15):
            events.append(_ev("ENTRY", f"W{i}", offset_s=i*60, store=store))
        for i in range(15):
            events.append(_ev("ZONE_ENTER", f"W{i}", "MAYBELLINE", offset_s=300+i*30, store=store))
        client.post("/events/ingest", json=events)
        r = client.get(f"/stores/{store}/insights")
        for ins in r.json()["insights"]:
            for field in ("insight_id", "priority", "category", "title", "detail", "impact"):
                assert field in ins, f"Insight missing field: {field}"


# ── OpenAPI ───────────────────────────────────────────────────────────────────

def test_new_endpoints_in_openapi(client):
    r = client.get("/openapi.json")
    assert r.status_code == 200
    paths = r.json()["paths"]
    assert any("revenue" in p for p in paths)
    assert any("journey" in p for p in paths)
    assert any("insights" in p for p in paths)
