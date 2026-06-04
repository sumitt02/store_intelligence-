"""
Real-time store metrics computation.

Conversion rate: visitor was in BILLING zone in the 5-minute window
before a POS transaction timestamp, for the same store.

All metrics exclude is_staff=true events.
"""
from __future__ import annotations

import csv
import os
from datetime import datetime, timezone, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import StoreMetrics, ZoneMetric

_default_pos = str(Path(__file__).parent.parent / "data" / "pos_transactions.csv")
POS_PATH = os.environ.get("POS_PATH", _default_pos)
CONVERSION_WINDOW_MINUTES = 5


def _load_pos_transactions(store_id: str) -> list[datetime]:
    """Load POS transaction timestamps for a store."""
    transactions: list[datetime] = []
    try:
        with open(POS_PATH, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row["store_id"] == store_id:
                    ts = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
                    transactions.append(ts)
    except FileNotFoundError:
        pass
    return transactions


def _parse_ts(ts_str: str) -> datetime:
    return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))


def compute_metrics(store_id: str, db: Session, date: Optional[str] = None) -> StoreMetrics:
    now = datetime.now(timezone.utc)
    if date:
        ref = datetime.fromisoformat(date).replace(tzinfo=timezone.utc)
        now = ref.replace(hour=23, minute=59, second=59)
        today_start = ref.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_str = today_start.isoformat().replace("+00:00", "Z")

    # Unique visitors today (excluding staff)
    unique_q = db.execute(text("""
        SELECT COUNT(DISTINCT visitor_id) as cnt
        FROM events
        WHERE store_id = :store_id
          AND event_type = 'ENTRY'
          AND is_staff = 0
          AND timestamp >= :today
    """), {"store_id": store_id, "today": today_str})
    unique_visitors = unique_q.scalar() or 0

    # Visitors who were in BILLING zone in 5min window before a transaction
    pos_txns = _load_pos_transactions(store_id)
    converted_visitors: set[str] = set()

    if pos_txns and unique_visitors > 0:
        for txn_ts in pos_txns:
            window_start = (txn_ts - timedelta(minutes=CONVERSION_WINDOW_MINUTES)).isoformat().replace("+00:00", "Z")
            window_end = txn_ts.isoformat().replace("+00:00", "Z")

            billing_q = db.execute(text("""
                SELECT DISTINCT visitor_id
                FROM events
                WHERE store_id = :store_id
                  AND zone_id IN ('BILLING', 'CASH_COUNTER')
                  AND is_staff = 0
                  AND timestamp BETWEEN :start AND :end
            """), {"store_id": store_id, "start": window_start, "end": window_end})

            for row in billing_q:
                converted_visitors.add(row[0])

    conversion_rate = len(converted_visitors) / unique_visitors if unique_visitors > 0 else 0.0

    # Avg dwell per zone (ZONE_DWELL events, today, excluding staff)
    dwell_q = db.execute(text("""
        SELECT zone_id, AVG(dwell_ms) as avg_dwell, COUNT(*) as cnt
        FROM events
        WHERE store_id = :store_id
          AND event_type IN ('ZONE_DWELL', 'ZONE_EXIT')
          AND is_staff = 0
          AND zone_id IS NOT NULL
          AND timestamp >= :today
        GROUP BY zone_id
    """), {"store_id": store_id, "today": today_str})

    zone_metrics = [
        ZoneMetric(
            zone_id=row[0],
            avg_dwell_ms=round(row[1] or 0, 2),
            visit_count=row[2] or 0,
        )
        for row in dwell_q
    ]

    # Current queue depth (BILLING_QUEUE_JOIN - BILLING_QUEUE_ABANDON/EXIT in last 30 min)
    thirty_min_ago = (now - timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
    queue_joins = db.execute(text("""
        SELECT COUNT(DISTINCT visitor_id)
        FROM events
        WHERE store_id = :store_id
          AND event_type = 'BILLING_QUEUE_JOIN'
          AND timestamp >= :since
    """), {"store_id": store_id, "since": thirty_min_ago}).scalar() or 0

    queue_exits = db.execute(text("""
        SELECT COUNT(DISTINCT visitor_id)
        FROM events
        WHERE store_id = :store_id
          AND event_type IN ('BILLING_QUEUE_ABANDON', 'EXIT')
          AND zone_id = 'BILLING'
          AND timestamp >= :since
    """), {"store_id": store_id, "since": thirty_min_ago}).scalar() or 0

    queue_depth = max(0, queue_joins - queue_exits)

    # Abandonment rate: BILLING_QUEUE_ABANDON / (BILLING_QUEUE_JOIN) today
    total_joins = db.execute(text("""
        SELECT COUNT(*) FROM events
        WHERE store_id = :store_id
          AND event_type = 'BILLING_QUEUE_JOIN'
          AND zone_id IN ('BILLING', 'CASH_COUNTER')
          AND timestamp >= :today
    """), {"store_id": store_id, "today": today_str}).scalar() or 0

    total_abandons = db.execute(text("""
        SELECT COUNT(*) FROM events
        WHERE store_id = :store_id
          AND event_type = 'BILLING_QUEUE_ABANDON'
          AND zone_id IN ('BILLING', 'CASH_COUNTER')
          AND timestamp >= :today
    """), {"store_id": store_id, "today": today_str}).scalar() or 0

    abandonment_rate = total_abandons / total_joins if total_joins > 0 else 0.0

    return StoreMetrics(
        store_id=store_id,
        as_of=now.isoformat().replace("+00:00", "Z"),
        unique_visitors=unique_visitors,
        conversion_rate=round(conversion_rate, 4),
        avg_dwell_per_zone=zone_metrics,
        queue_depth=queue_depth,
        abandonment_rate=round(abandonment_rate, 4),
    )
