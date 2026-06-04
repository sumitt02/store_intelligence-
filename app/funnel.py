"""
Conversion funnel: Entry → Zone Visit → Billing Queue → Purchase.

Session is the unit — re-entries must not double-count a visitor.
We group by visitor_id, taking the first ENTRY per session window.

Note: CASH_COUNTER and BILLING are treated as equivalent billing zones.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from sqlalchemy import text
from sqlalchemy.orm import Session

from typing import Optional

from app.models import StoreFunnel, FunnelStage

BILLING_ZONES = ("'BILLING'", "'CASH_COUNTER'")


def compute_funnel(store_id: str, db: Session, date: Optional[str] = None) -> StoreFunnel:
    now = datetime.now(timezone.utc)
    if date:
        ref = datetime.fromisoformat(date).replace(tzinfo=timezone.utc)
        today_str = ref.replace(hour=0, minute=0, second=0, microsecond=0).isoformat().replace("+00:00", "Z")
    else:
        today_str = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat().replace("+00:00", "Z")

    # Stage 1: unique visitor sessions that entered (ENTRY only, not REENTRY, not staff)
    entered = db.execute(text("""
        SELECT COUNT(DISTINCT visitor_id)
        FROM events
        WHERE store_id = :store_id
          AND event_type = 'ENTRY'
          AND is_staff = 0
          AND timestamp >= :today
    """), {"store_id": store_id, "today": today_str}).scalar() or 0

    # Stage 2: unique visitors who visited at least one product zone
    visited_zone = db.execute(text("""
        SELECT COUNT(DISTINCT visitor_id)
        FROM events
        WHERE store_id = :store_id
          AND event_type IN ('ZONE_ENTER', 'ZONE_DWELL')
          AND is_staff = 0
          AND zone_id NOT IN ('ENTRY_EXIT', 'BILLING')
          AND zone_id IS NOT NULL
          AND timestamp >= :today
    """), {"store_id": store_id, "today": today_str}).scalar() or 0

    billing_in = ", ".join(BILLING_ZONES)

    # Stage 3: unique visitors who joined billing queue or entered BILLING/CASH_COUNTER
    reached_billing = db.execute(text(f"""
        SELECT COUNT(DISTINCT visitor_id)
        FROM events
        WHERE store_id = :store_id
          AND event_type IN ('BILLING_QUEUE_JOIN', 'ZONE_ENTER')
          AND zone_id IN ({billing_in})
          AND is_staff = 0
          AND timestamp >= :today
    """), {"store_id": store_id, "today": today_str}).scalar() or 0

    # Stage 4: purchased = billing zone exit with no subsequent BILLING_QUEUE_ABANDON
    purchased = db.execute(text(f"""
        SELECT COUNT(DISTINCT visitor_id)
        FROM events
        WHERE store_id = :store_id
          AND event_type = 'ZONE_EXIT'
          AND zone_id IN ({billing_in})
          AND is_staff = 0
          AND timestamp >= :today
          AND visitor_id NOT IN (
              SELECT DISTINCT visitor_id FROM events
              WHERE store_id = :store_id
                AND event_type = 'BILLING_QUEUE_ABANDON'
                AND timestamp >= :today
          )
    """), {"store_id": store_id, "today": today_str}).scalar() or 0

    def drop_off(prev: int, curr: int) -> float:
        if prev == 0:
            return 0.0
        return round((prev - curr) / prev * 100, 2)

    stages = [
        FunnelStage(stage="ENTRY", count=entered, drop_off_pct=0.0),
        FunnelStage(stage="ZONE_VISIT", count=visited_zone, drop_off_pct=drop_off(entered, visited_zone)),
        FunnelStage(stage="BILLING_QUEUE", count=reached_billing, drop_off_pct=drop_off(visited_zone, reached_billing)),
        FunnelStage(stage="PURCHASE", count=purchased, drop_off_pct=drop_off(reached_billing, purchased)),
    ]

    return StoreFunnel(
        store_id=store_id,
        as_of=now.isoformat().replace("+00:00", "Z"),
        stages=stages,
    )
