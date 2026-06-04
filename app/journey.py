"""
Customer Journey Analytics

- Zone path sequences per visitor session
- Average journey length
- Most common zone transitions (Markov-style)
- Brand affinity: which zones are co-visited
- Entry-to-exit path summary
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from itertools import combinations
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

EXCLUDE_ZONES = {"ENTRY_EXIT", "BILLING", "CASH_COUNTER", "STOCKROOM"}


def compute_journey(store_id: str, db: Session, date: Optional[str] = None) -> dict:
    now = datetime.now(timezone.utc)
    date_str = date or now.strftime("%Y-%m-%d")
    today_start = datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc)
    today_str = today_start.isoformat().replace("+00:00", "Z")

    # Fetch all ZONE_ENTER events ordered by visitor + time
    rows = db.execute(text("""
        SELECT visitor_id, zone_id, timestamp
        FROM events
        WHERE store_id = :store_id
          AND event_type = 'ZONE_ENTER'
          AND zone_id IS NOT NULL
          AND is_staff = 0
          AND timestamp >= :today
        ORDER BY visitor_id, timestamp
    """), {"store_id": store_id, "today": today_str}).fetchall()

    # Build per-visitor zone sequences
    visitor_paths: dict[str, list[str]] = defaultdict(list)
    for visitor_id, zone_id, _ in rows:
        if zone_id not in EXCLUDE_ZONES:
            if not visitor_paths[visitor_id] or visitor_paths[visitor_id][-1] != zone_id:
                visitor_paths[visitor_id].append(zone_id)

    # Transition counts: A → B
    transitions: dict[tuple, int] = defaultdict(int)
    for path in visitor_paths.values():
        for i in range(len(path) - 1):
            transitions[(path[i], path[i + 1])] += 1

    # Co-visit affinity: zones visited together in same session
    co_visit: dict[tuple, int] = defaultdict(int)
    for path in visitor_paths.values():
        unique_zones = list(dict.fromkeys(path))  # preserve order, dedupe
        for a, b in combinations(sorted(unique_zones), 2):
            co_visit[(a, b)] += 1

    # Journey length distribution
    lengths = [len(p) for p in visitor_paths.values() if p]
    avg_journey_length = sum(lengths) / len(lengths) if lengths else 0.0

    length_dist: dict[str, int] = defaultdict(int)
    for l in lengths:
        bucket = f"{l}" if l <= 5 else "6+"
        length_dist[bucket] += 1

    # Top transitions
    top_transitions = [
        {"from": a, "to": b, "count": cnt}
        for (a, b), cnt in sorted(transitions.items(), key=lambda x: -x[1])[:15]
    ]

    # Top co-visits
    top_affinities = [
        {"zone_a": a, "zone_b": b, "co_visits": cnt}
        for (a, b), cnt in sorted(co_visit.items(), key=lambda x: -x[1])[:12]
    ]

    # Most common first zone (what customers browse first)
    first_zones: dict[str, int] = defaultdict(int)
    for path in visitor_paths.values():
        if path:
            first_zones[path[0]] += 1

    # Most common last zone before checkout
    last_zones: dict[str, int] = defaultdict(int)
    for path in visitor_paths.values():
        if path:
            last_zones[path[-1]] += 1

    return {
        "store_id": store_id,
        "date": date_str,
        "as_of": now.isoformat().replace("+00:00", "Z"),
        "unique_sessions_with_zone_visits": len(visitor_paths),
        "avg_zones_per_session": round(avg_journey_length, 2),
        "journey_length_distribution": dict(sorted(length_dist.items())),
        "top_entry_zones": dict(sorted(first_zones.items(), key=lambda x: -x[1])[:5]),
        "top_exit_zones": dict(sorted(last_zones.items(), key=lambda x: -x[1])[:5]),
        "top_transitions": top_transitions,
        "brand_affinity": top_affinities,
    }
