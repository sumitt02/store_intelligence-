"""
Health check endpoint.
Returns per-store feed freshness. STALE_FEED if last event > 10 min ago.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import HealthResponse, StoreHealth

STALE_THRESHOLD_MINUTES = 10


def get_health(db: Session) -> HealthResponse:
    now = datetime.now(timezone.utc)
    now_str = now.isoformat().replace("+00:00", "Z")

    stores_q = db.execute(text("""
        SELECT store_id, MAX(timestamp) as last_ts
        FROM events
        GROUP BY store_id
    """))

    store_healths: list[StoreHealth] = []
    overall_ok = True

    for row in stores_q:
        store_id, last_ts = row
        if last_ts is None:
            store_healths.append(StoreHealth(
                store_id=store_id,
                status="NO_DATA",
                last_event_timestamp=None,
                lag_minutes=None,
            ))
            overall_ok = False
            continue

        last_dt = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
        lag_minutes = (now - last_dt).total_seconds() / 60

        if lag_minutes > STALE_THRESHOLD_MINUTES:
            status = "STALE_FEED"
            overall_ok = False
        else:
            status = "OK"

        store_healths.append(StoreHealth(
            store_id=store_id,
            status=status,
            last_event_timestamp=last_ts,
            lag_minutes=round(lag_minutes, 2),
        ))

    return HealthResponse(
        status="ok" if overall_ok else "degraded",
        checked_at=now_str,
        stores=store_healths,
    )
