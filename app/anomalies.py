"""
Anomaly detection for real-time store operations.

Three anomaly types:
  BILLING_QUEUE_SPIKE  — queue depth > threshold vs 7-day avg (CRITICAL)
  CONVERSION_DROP      — today's conversion rate < 7-day avg by >20% (WARN/CRITICAL)
  DEAD_ZONE            — no visits to a zone in last 30 minutes during open hours (INFO/WARN)
  STALE_FEED           — no events from a store in 10+ minutes (CRITICAL)
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import Anomaly, StoreAnomalies

QUEUE_SPIKE_THRESHOLD = 5      # queue depth > 5 = spike
CONVERSION_DROP_THRESHOLD = 0.20  # 20% below 7-day avg
DEAD_ZONE_MINUTES = 30
STALE_FEED_MINUTES = 10


def _now_str() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def detect_anomalies(store_id: str, db: Session) -> StoreAnomalies:
    now = datetime.now(timezone.utc)
    anomalies: list[Anomaly] = []

    # ── BILLING_QUEUE_SPIKE ─────────────────────────────────────────────────
    five_min_ago = (now - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")

    current_queue = db.execute(text("""
        SELECT COUNT(DISTINCT visitor_id)
        FROM events
        WHERE store_id = :store_id
          AND event_type = 'BILLING_QUEUE_JOIN'
          AND timestamp >= :since
          AND visitor_id NOT IN (
              SELECT DISTINCT visitor_id FROM events
              WHERE store_id = :store_id
                AND event_type IN ('BILLING_QUEUE_ABANDON', 'EXIT')
                AND timestamp >= :since
          )
    """), {"store_id": store_id, "since": five_min_ago}).scalar() or 0

    # 7-day average queue depth (hourly buckets)
    seven_days_ago = (now - timedelta(days=7)).isoformat().replace("+00:00", "Z")
    avg_queue = db.execute(text("""
        SELECT AVG(daily_joins) FROM (
            SELECT DATE(timestamp) as day, COUNT(*) as daily_joins
            FROM events
            WHERE store_id = :store_id
              AND event_type = 'BILLING_QUEUE_JOIN'
              AND timestamp >= :since
            GROUP BY DATE(timestamp)
        )
    """), {"store_id": store_id, "since": seven_days_ago}).scalar() or 0

    if current_queue >= QUEUE_SPIKE_THRESHOLD:
        severity = "CRITICAL" if current_queue >= QUEUE_SPIKE_THRESHOLD * 2 else "WARN"
        anomalies.append(Anomaly(
            anomaly_id=str(uuid.uuid4()),
            store_id=store_id,
            anomaly_type="BILLING_QUEUE_SPIKE",
            severity=severity,
            detected_at=_now_str(),
            description=f"Billing queue depth is {current_queue} (threshold: {QUEUE_SPIKE_THRESHOLD})",
            suggested_action="Open additional billing counter or redirect floor staff to billing",
            metadata={"current_queue_depth": current_queue, "threshold": QUEUE_SPIKE_THRESHOLD},
        ))

    # ── CONVERSION_DROP ─────────────────────────────────────────────────────
    today_str = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat().replace("+00:00", "Z")

    today_visitors = db.execute(text("""
        SELECT COUNT(DISTINCT visitor_id)
        FROM events
        WHERE store_id = :store_id
          AND event_type = 'ENTRY'
          AND is_staff = 0
          AND timestamp >= :today
    """), {"store_id": store_id, "today": today_str}).scalar() or 0

    today_purchased = db.execute(text("""
        SELECT COUNT(DISTINCT visitor_id)
        FROM events
        WHERE store_id = :store_id
          AND event_type = 'ZONE_EXIT'
          AND zone_id = 'BILLING'
          AND is_staff = 0
          AND timestamp >= :today
    """), {"store_id": store_id, "today": today_str}).scalar() or 0

    today_rate = today_purchased / today_visitors if today_visitors > 0 else None

    # 7-day historical rate
    hist_visitors = db.execute(text("""
        SELECT COUNT(DISTINCT visitor_id)
        FROM events
        WHERE store_id = :store_id
          AND event_type = 'ENTRY'
          AND is_staff = 0
          AND timestamp >= :since
          AND timestamp < :today
    """), {"store_id": store_id, "since": seven_days_ago, "today": today_str}).scalar() or 0

    hist_purchased = db.execute(text("""
        SELECT COUNT(DISTINCT visitor_id)
        FROM events
        WHERE store_id = :store_id
          AND event_type = 'ZONE_EXIT'
          AND zone_id = 'BILLING'
          AND is_staff = 0
          AND timestamp >= :since
          AND timestamp < :today
    """), {"store_id": store_id, "since": seven_days_ago, "today": today_str}).scalar() or 0

    hist_rate = hist_purchased / hist_visitors if hist_visitors > 0 else None

    if today_rate is not None and hist_rate is not None and hist_rate > 0:
        drop = (hist_rate - today_rate) / hist_rate
        if drop >= CONVERSION_DROP_THRESHOLD:
            severity = "CRITICAL" if drop >= 0.4 else "WARN"
            anomalies.append(Anomaly(
                anomaly_id=str(uuid.uuid4()),
                store_id=store_id,
                anomaly_type="CONVERSION_DROP",
                severity=severity,
                detected_at=_now_str(),
                description=(
                    f"Conversion rate {today_rate:.1%} is {drop:.0%} below "
                    f"7-day average {hist_rate:.1%}"
                ),
                suggested_action="Review product zone dwell times and check for billing queue abandonment",
                metadata={
                    "today_rate": round(today_rate, 4),
                    "historical_rate": round(hist_rate, 4),
                    "drop_pct": round(drop * 100, 2),
                },
            ))

    # ── DEAD_ZONE ────────────────────────────────────────────────────────────
    if 10 <= now.hour <= 21:  # only during store hours
        thirty_min_ago = (now - timedelta(minutes=DEAD_ZONE_MINUTES)).isoformat().replace("+00:00", "Z")

        active_zones = db.execute(text("""
            SELECT DISTINCT zone_id
            FROM events
            WHERE store_id = :store_id
              AND zone_id NOT IN ('ENTRY_EXIT', 'BILLING')
              AND zone_id IS NOT NULL
              AND is_staff = 0
              AND timestamp >= :since
        """), {"store_id": store_id, "since": thirty_min_ago})

        recently_active = {row[0] for row in active_zones}

        all_zones = db.execute(text("""
            SELECT DISTINCT zone_id
            FROM events
            WHERE store_id = :store_id
              AND zone_id NOT IN ('ENTRY_EXIT', 'BILLING')
              AND zone_id IS NOT NULL
        """), {"store_id": store_id})

        all_known = {row[0] for row in all_zones}
        dead_zones = all_known - recently_active

        for zone in dead_zones:
            severity = "WARN" if len(dead_zones) > 2 else "INFO"
            anomalies.append(Anomaly(
                anomaly_id=str(uuid.uuid4()),
                store_id=store_id,
                anomaly_type="DEAD_ZONE",
                severity=severity,
                detected_at=_now_str(),
                description=f"Zone '{zone}' has had no customer visits in the past {DEAD_ZONE_MINUTES} minutes",
                suggested_action=f"Check if zone '{zone}' display is attracting attention; consider staff intervention",
                metadata={"zone_id": zone, "no_visit_minutes": DEAD_ZONE_MINUTES},
            ))

    # ── STALE_FEED ───────────────────────────────────────────────────────────
    stale_cutoff = (now - timedelta(minutes=STALE_FEED_MINUTES)).isoformat().replace("+00:00", "Z")
    last_event = db.execute(text("""
        SELECT MAX(timestamp) FROM events WHERE store_id = :store_id
    """), {"store_id": store_id}).scalar()

    if last_event:
        lag_minutes = (now - datetime.fromisoformat(last_event.replace("Z", "+00:00"))).total_seconds() / 60
        if lag_minutes > STALE_FEED_MINUTES:
            anomalies.append(Anomaly(
                anomaly_id=str(uuid.uuid4()),
                store_id=store_id,
                anomaly_type="STALE_FEED",
                severity="CRITICAL",
                detected_at=_now_str(),
                description=f"No events received from {store_id} in {lag_minutes:.1f} minutes",
                suggested_action="Check camera connectivity and detection pipeline health",
                metadata={"last_event_at": last_event, "lag_minutes": round(lag_minutes, 2)},
            ))

    return StoreAnomalies(
        store_id=store_id,
        as_of=_now_str(),
        anomalies=anomalies,
    )
