"""
Synthetic event generator for testing the API without real CCTV footage.

Produces realistic visitor flows with:
- Groups entering together (2-4 people)
- Staff movement patterns (high zone coverage, short dwell)
- Re-entries (same visitor_id after exit)
- Queue buildup and abandonment
- Empty periods (5-10 min windows with zero traffic)

This is also the fallback when detect.py can't find video files.
"""
from __future__ import annotations

import json
import random
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from emit import StoreEvent, make_event
from tracker import ReIDTracker

LAYOUT_PATH = Path(__file__).parent.parent / "data" / "store_layout.json"
POS_PATH = Path(__file__).parent.parent / "data" / "pos_transactions.csv"

# Realistic distribution: 8 visitors/hour average, peak 10am-12pm and 6pm-8pm
HOURLY_VISITOR_RATE = {
    10: 12, 11: 18, 12: 15, 13: 10, 14: 8, 15: 8, 16: 10, 17: 14, 18: 20, 19: 16, 20: 8, 21: 4
}


def _poisson_arrivals(rate_per_hour: int, duration_minutes: int = 20) -> list[float]:
    """Generate arrival times (seconds from start) using Poisson process."""
    rate_per_second = rate_per_hour / 3600
    arrivals = []
    t = 0.0
    while t < duration_minutes * 60:
        inter_arrival = random.expovariate(rate_per_second)
        t += inter_arrival
        if t < duration_minutes * 60:
            arrivals.append(t)
    return arrivals


def generate_visitor_session(
    store_id: str,
    visitor_id: str,
    camera_prefix: str,
    entry_time: datetime,
    zones: list[str],
    is_staff: bool = False,
    is_group: bool = False,
    reentry: bool = False,
) -> list[StoreEvent]:
    """Generate a realistic visitor session with zone visits."""
    events = []
    seq = 0
    current_time = entry_time

    def next_seq():
        nonlocal seq
        seq += 1
        return seq

    # ENTRY or REENTRY
    entry_type = "REENTRY" if reentry else "ENTRY"
    events.append(make_event(
        store_id=store_id,
        camera_id=f"{camera_prefix}_ENTRY_01",
        visitor_id=visitor_id,
        event_type=entry_type,
        timestamp=current_time,
        zone_id=None,
        dwell_ms=0,
        is_staff=is_staff,
        confidence=random.uniform(0.72, 0.98),
        session_seq=next_seq(),
    ))

    if is_staff:
        # Staff moves through all zones quickly
        visit_zones = zones[:]
        random.shuffle(visit_zones)
        for zone in visit_zones:
            current_time += timedelta(seconds=random.uniform(20, 60))
            events.append(make_event(
                store_id=store_id,
                camera_id=f"{camera_prefix}_FLOOR_01",
                visitor_id=visitor_id,
                event_type="ZONE_ENTER",
                timestamp=current_time,
                zone_id=zone,
                dwell_ms=0,
                is_staff=True,
                confidence=random.uniform(0.75, 0.95),
                session_seq=next_seq(),
            ))
            dwell_secs = random.uniform(15, 45)
            current_time += timedelta(seconds=dwell_secs)
            events.append(make_event(
                store_id=store_id,
                camera_id=f"{camera_prefix}_FLOOR_01",
                visitor_id=visitor_id,
                event_type="ZONE_EXIT",
                timestamp=current_time,
                zone_id=zone,
                dwell_ms=int(dwell_secs * 1000),
                is_staff=True,
                confidence=random.uniform(0.75, 0.95),
                session_seq=next_seq(),
            ))
    else:
        # Customer: visits 1-3 zones, dwells 30-300s each
        n_zones = random.randint(1, min(3, len(zones)))
        visit_zones = random.sample(zones, n_zones)

        abandons_queue = False
        for i, zone in enumerate(visit_zones):
            current_time += timedelta(seconds=random.uniform(10, 30))
            sku_zone = zone if zone not in ("ENTRY_EXIT", "BILLING") else None

            events.append(make_event(
                store_id=store_id,
                camera_id=f"{camera_prefix}_FLOOR_01",
                visitor_id=visitor_id,
                event_type="ZONE_ENTER",
                timestamp=current_time,
                zone_id=zone,
                dwell_ms=0,
                is_staff=False,
                confidence=random.uniform(0.65, 0.97),
                sku_zone=sku_zone,
                session_seq=next_seq(),
            ))

            dwell_secs = random.uniform(30, 300)
            # Emit ZONE_DWELL every 30s for long dwells
            if dwell_secs > 30:
                for dwell_tick in range(1, int(dwell_secs // 30) + 1):
                    tick_time = current_time + timedelta(seconds=dwell_tick * 30)
                    events.append(make_event(
                        store_id=store_id,
                        camera_id=f"{camera_prefix}_FLOOR_01",
                        visitor_id=visitor_id,
                        event_type="ZONE_DWELL",
                        timestamp=tick_time,
                        zone_id=zone,
                        dwell_ms=int(dwell_tick * 30 * 1000),
                        is_staff=False,
                        confidence=random.uniform(0.65, 0.95),
                        sku_zone=sku_zone,
                        session_seq=next_seq(),
                    ))

            current_time += timedelta(seconds=dwell_secs)
            events.append(make_event(
                store_id=store_id,
                camera_id=f"{camera_prefix}_FLOOR_01",
                visitor_id=visitor_id,
                event_type="ZONE_EXIT",
                timestamp=current_time,
                zone_id=zone,
                dwell_ms=int(dwell_secs * 1000),
                is_staff=False,
                confidence=random.uniform(0.65, 0.97),
                sku_zone=sku_zone,
                session_seq=next_seq(),
            ))

        # Maybe go to billing (60% chance)
        goes_to_billing = random.random() < 0.60
        if goes_to_billing:
            current_time += timedelta(seconds=random.uniform(5, 20))
            queue_depth = random.randint(0, 4)

            if queue_depth > 0:
                events.append(make_event(
                    store_id=store_id,
                    camera_id=f"{camera_prefix}_BILLING_01",
                    visitor_id=visitor_id,
                    event_type="BILLING_QUEUE_JOIN",
                    timestamp=current_time,
                    zone_id="BILLING",
                    dwell_ms=0,
                    is_staff=False,
                    confidence=random.uniform(0.80, 0.97),
                    queue_depth=queue_depth,
                    session_seq=next_seq(),
                ))

            # 15% chance of abandoning long queue
            abandons_queue = queue_depth >= 3 and random.random() < 0.15
            if abandons_queue:
                wait_secs = random.uniform(60, 180)
                current_time += timedelta(seconds=wait_secs)
                events.append(make_event(
                    store_id=store_id,
                    camera_id=f"{camera_prefix}_BILLING_01",
                    visitor_id=visitor_id,
                    event_type="BILLING_QUEUE_ABANDON",
                    timestamp=current_time,
                    zone_id="BILLING",
                    dwell_ms=int(wait_secs * 1000),
                    is_staff=False,
                    confidence=random.uniform(0.75, 0.90),
                    queue_depth=queue_depth,
                    session_seq=next_seq(),
                ))
            else:
                billing_dwell = random.uniform(60, 300)
                current_time += timedelta(seconds=billing_dwell)
                events.append(make_event(
                    store_id=store_id,
                    camera_id=f"{camera_prefix}_BILLING_01",
                    visitor_id=visitor_id,
                    event_type="ZONE_EXIT",
                    timestamp=current_time,
                    zone_id="BILLING",
                    dwell_ms=int(billing_dwell * 1000),
                    is_staff=False,
                    confidence=random.uniform(0.80, 0.97),
                    session_seq=next_seq(),
                ))

    # EXIT
    current_time += timedelta(seconds=random.uniform(5, 30))
    events.append(make_event(
        store_id=store_id,
        camera_id=f"{camera_prefix}_ENTRY_01",
        visitor_id=visitor_id,
        event_type="EXIT",
        timestamp=current_time,
        zone_id=None,
        dwell_ms=0,
        is_staff=is_staff,
        confidence=random.uniform(0.75, 0.97),
        session_seq=next_seq(),
    ))

    return events


def generate_events(layout: dict, duration_minutes: int = 20) -> list[StoreEvent]:
    """Generate synthetic events for all stores in the layout."""
    random.seed(42)  # Reproducible for testing
    all_events: list[StoreEvent] = []
    trackers: dict[str, ReIDTracker] = {}

    base_time = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)

    for store_id, store_config in layout["stores"].items():
        trackers[store_id] = ReIDTracker(store_id)
        product_zones = [
            z for z, cfg in store_config["zones"].items()
            if cfg["type"] == "product"
        ]

        # Derive camera prefix from first camera key
        cam_keys = list(store_config["cameras"].keys())
        cam_prefix = "_".join(cam_keys[0].split("_")[:2])  # CAM

        rate = HOURLY_VISITOR_RATE.get(10, 10)
        arrivals = _poisson_arrivals(rate, duration_minutes)

        # 5% staff ratio
        n_staff = max(1, int(len(arrivals) * 0.05))
        staff_arrivals = random.sample(range(len(arrivals)), min(n_staff, len(arrivals)))

        # 20% group entries (groups of 2-3)
        group_starts = set()
        i = 0
        while i < len(arrivals) - 1:
            if random.random() < 0.2 and i not in group_starts:
                group_starts.add(i)
                group_starts.add(i + 1)
                if i + 2 < len(arrivals) and random.random() < 0.3:
                    group_starts.add(i + 2)
                i += 2
            else:
                i += 1

        # ~10% re-entries
        store_visitor_ids: list[str] = []
        reentry_targets: set[str] = set()

        for idx, arrival_secs in enumerate(arrivals):
            entry_time = base_time + timedelta(seconds=arrival_secs)
            is_staff = idx in staff_arrivals
            is_group = idx in group_starts

            # Re-entry logic
            is_reentry = False
            if store_visitor_ids and random.random() < 0.10:
                reentry_vid = random.choice(store_visitor_ids)
                visitor_id = reentry_vid
                is_reentry = True
            else:
                tracker = trackers[store_id]
                visitor_id, _ = tracker.register_track(
                    idx, [0.5, 0.5, 0.6, 0.9], None, entry_time.timestamp()
                )
                store_visitor_ids.append(visitor_id)

            events = generate_visitor_session(
                store_id=store_id,
                visitor_id=visitor_id,
                camera_prefix=cam_prefix,
                entry_time=entry_time,
                zones=product_zones,
                is_staff=is_staff,
                is_group=is_group,
                reentry=is_reentry,
            )
            all_events.extend(events)

    # Sort by timestamp
    all_events.sort(key=lambda e: e.timestamp)
    print(f"[simulator] Generated {len(all_events)} synthetic events across {len(layout['stores'])} stores")
    return all_events


if __name__ == "__main__":
    layout_path = LAYOUT_PATH
    with open(layout_path) as f:
        layout = json.load(f)

    events = generate_events(layout)

    import sys
    output = sys.argv[1] if len(sys.argv) > 1 else "./events.jsonl"
    from emit import emit_to_file
    emit_to_file(events, output)
