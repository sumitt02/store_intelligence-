"""
Revenue Intelligence — ties POS basket data to footfall metrics.

Computes:
  - revenue_per_visitor: total POS revenue / unique visitors
  - avg_basket_value: mean transaction value for the day
  - top_revenue_hours: hourly revenue distribution
  - zone_revenue_affinity: zones visited by customers who purchased
  - peak_transaction_hour: hour with most transactions
"""
from __future__ import annotations

import csv
import os
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

_default_pos = str(Path(__file__).parent.parent / "data" / "pos_transactions.csv")
POS_PATH = os.environ.get("POS_PATH", _default_pos)
CONVERSION_WINDOW_MINUTES = 5


def _load_pos(store_id: str, date_str: Optional[str] = None) -> list[dict]:
    txns = []
    try:
        with open(POS_PATH, newline="") as f:
            for row in csv.DictReader(f):
                if row["store_id"] != store_id:
                    continue
                ts = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
                if date_str and ts.strftime("%Y-%m-%d") != date_str:
                    continue
                txns.append({
                    "timestamp": ts,
                    "basket_value": float(row["basket_value_inr"]),
                    "transaction_id": row["transaction_id"],
                })
    except FileNotFoundError:
        pass
    return txns


def compute_revenue(store_id: str, db: Session, date: Optional[str] = None) -> dict:
    now = datetime.now(timezone.utc)
    date_str = date or now.strftime("%Y-%m-%d")
    today_start = datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc)
    today_str = today_start.isoformat().replace("+00:00", "Z")

    txns = _load_pos(store_id, date_str)
    total_revenue = sum(t["basket_value"] for t in txns)
    avg_basket = total_revenue / len(txns) if txns else 0.0

    # Unique visitors that day
    unique_visitors = db.execute(text("""
        SELECT COUNT(DISTINCT visitor_id) FROM events
        WHERE store_id = :store_id AND event_type = 'ENTRY'
          AND is_staff = 0 AND timestamp >= :today
    """), {"store_id": store_id, "today": today_str}).scalar() or 0

    revenue_per_visitor = total_revenue / unique_visitors if unique_visitors > 0 else 0.0

    # Hourly revenue breakdown
    hourly: dict[int, float] = defaultdict(float)
    hourly_count: dict[int, int] = defaultdict(int)
    for t in txns:
        h = t["timestamp"].hour
        hourly[h] += t["basket_value"]
        hourly_count[h] += 1

    peak_hour = max(hourly, key=hourly.get) if hourly else None

    # Zone affinity: zones visited by converted visitors (in CASH_COUNTER/BILLING within 5min before txn)
    zone_affinity: dict[str, int] = defaultdict(int)
    converted_visitors: set[str] = set()
    for t in txns:
        window_start = (t["timestamp"] - timedelta(minutes=CONVERSION_WINDOW_MINUTES)).isoformat().replace("+00:00", "Z")
        window_end = t["timestamp"].isoformat().replace("+00:00", "Z")
        rows = db.execute(text("""
            SELECT DISTINCT visitor_id FROM events
            WHERE store_id = :store_id
              AND zone_id IN ('BILLING', 'CASH_COUNTER')
              AND is_staff = 0
              AND timestamp BETWEEN :start AND :end
        """), {"store_id": store_id, "start": window_start, "end": window_end}).fetchall()
        for row in rows:
            converted_visitors.add(row[0])

    if converted_visitors:
        placeholders = ",".join(f"'{v}'" for v in converted_visitors)
        zone_rows = db.execute(text(f"""
            SELECT zone_id, COUNT(*) as cnt FROM events
            WHERE store_id = :store_id
              AND event_type = 'ZONE_ENTER'
              AND zone_id NOT IN ('ENTRY_EXIT', 'BILLING', 'CASH_COUNTER')
              AND zone_id IS NOT NULL
              AND is_staff = 0
              AND visitor_id IN ({placeholders})
              AND timestamp >= :today
            GROUP BY zone_id ORDER BY cnt DESC
        """), {"store_id": store_id, "today": today_str}).fetchall()
        for row in zone_rows:
            zone_affinity[row[0]] = row[1]

    # Transaction timeline (for sparkline)
    timeline = [
        {"hour": h, "revenue": round(hourly[h], 2), "transactions": hourly_count[h]}
        for h in sorted(hourly.keys())
    ]

    return {
        "store_id": store_id,
        "date": date_str,
        "as_of": now.isoformat().replace("+00:00", "Z"),
        "total_revenue_inr": round(total_revenue, 2),
        "transaction_count": len(txns),
        "avg_basket_value_inr": round(avg_basket, 2),
        "unique_visitors": unique_visitors,
        "revenue_per_visitor_inr": round(revenue_per_visitor, 2),
        "converted_visitors": len(converted_visitors),
        "conversion_rate_pct": round(len(converted_visitors) / unique_visitors * 100, 1) if unique_visitors else 0.0,
        "peak_transaction_hour": peak_hour,
        "hourly_timeline": timeline,
        "pre_purchase_zones": dict(sorted(zone_affinity.items(), key=lambda x: -x[1])[:8]),
    }
