"""
Zone heatmap: visit frequency + average dwell, normalised 0-100.
Flags low-data windows (fewer than 20 sessions) so the UI can show confidence level.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from typing import Optional

from app.models import StoreHeatmap, HeatmapZone

LOW_DATA_SESSION_THRESHOLD = 20


def compute_heatmap(store_id: str, db: Session, date: Optional[str] = None) -> StoreHeatmap:
    now = datetime.now(timezone.utc)
    if date:
        ref = datetime.fromisoformat(date).replace(tzinfo=timezone.utc)
        today_str = ref.replace(hour=0, minute=0, second=0, microsecond=0).isoformat().replace("+00:00", "Z")
    else:
        today_str = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat().replace("+00:00", "Z")

    # Count unique sessions today (for confidence flag)
    session_count = db.execute(text("""
        SELECT COUNT(DISTINCT visitor_id)
        FROM events
        WHERE store_id = :store_id
          AND event_type = 'ENTRY'
          AND is_staff = 0
          AND timestamp >= :today
    """), {"store_id": store_id, "today": today_str}).scalar() or 0

    low_confidence = session_count < LOW_DATA_SESSION_THRESHOLD

    # Zone visit frequency and avg dwell
    zone_q = db.execute(text("""
        SELECT
            zone_id,
            COUNT(*) as visit_count,
            AVG(dwell_ms) as avg_dwell
        FROM events
        WHERE store_id = :store_id
          AND event_type IN ('ZONE_ENTER', 'ZONE_EXIT', 'ZONE_DWELL')
          AND is_staff = 0
          AND zone_id NOT IN ('ENTRY_EXIT')
          AND zone_id IS NOT NULL
          AND timestamp >= :today
        GROUP BY zone_id
        ORDER BY visit_count DESC
    """), {"store_id": store_id, "today": today_str})

    rows = list(zone_q)

    if not rows:
        return StoreHeatmap(
            store_id=store_id,
            as_of=now.isoformat().replace("+00:00", "Z"),
            zones=[],
        )

    max_visits = max(r[1] for r in rows) or 1
    max_dwell = max(r[2] or 0 for r in rows) or 1

    zones = []
    for zone_id, visit_count, avg_dwell in rows:
        avg_dwell = avg_dwell or 0
        # Score: 60% visits + 40% dwell, normalised 0-100
        score = round(
            0.6 * (visit_count / max_visits) * 100 +
            0.4 * (avg_dwell / max_dwell) * 100,
            1,
        )
        zones.append(HeatmapZone(
            zone_id=zone_id,
            visit_frequency=visit_count,
            avg_dwell_ms=round(avg_dwell, 2),
            score=score,
            data_confidence=not low_confidence,
        ))

    return StoreHeatmap(
        store_id=store_id,
        as_of=now.isoformat().replace("+00:00", "Z"),
        zones=zones,
    )
