"""
Event schema definition and emission utilities.
All events from the detection layer must conform to this schema before being sent to the API.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Literal, Optional
from dataclasses import dataclass, asdict, field


EVENT_TYPES = Literal[
    "ENTRY", "EXIT", "ZONE_ENTER", "ZONE_EXIT",
    "ZONE_DWELL", "BILLING_QUEUE_JOIN", "BILLING_QUEUE_ABANDON", "REENTRY"
]


@dataclass
class EventMetadata:
    queue_depth: Optional[int] = None
    sku_zone: Optional[str] = None
    session_seq: int = 0


@dataclass
class StoreEvent:
    store_id: str
    camera_id: str
    visitor_id: str
    event_type: str
    timestamp: str
    zone_id: Optional[str]
    dwell_ms: int
    is_staff: bool
    confidence: float
    metadata: EventMetadata
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict())


def make_event(
    store_id: str,
    camera_id: str,
    visitor_id: str,
    event_type: str,
    timestamp: datetime,
    zone_id: Optional[str] = None,
    dwell_ms: int = 0,
    is_staff: bool = False,
    confidence: float = 1.0,
    queue_depth: Optional[int] = None,
    sku_zone: Optional[str] = None,
    session_seq: int = 0,
) -> StoreEvent:
    return StoreEvent(
        store_id=store_id,
        camera_id=camera_id,
        visitor_id=visitor_id,
        event_type=event_type,
        timestamp=timestamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
        zone_id=zone_id,
        dwell_ms=dwell_ms,
        is_staff=is_staff,
        confidence=confidence,
        metadata=EventMetadata(
            queue_depth=queue_depth,
            sku_zone=sku_zone,
            session_seq=session_seq,
        ),
    )


def emit_to_file(events: list[StoreEvent], path: str) -> None:
    with open(path, "w") as f:
        for e in events:
            f.write(e.to_json() + "\n")
    print(f"[emit] Wrote {len(events)} events to {path}")


def emit_to_api(events: list[StoreEvent], api_url: str, batch_size: int = 500) -> None:
    import requests

    batches = [events[i:i+batch_size] for i in range(0, len(events), batch_size)]
    for batch in batches:
        payload = [e.to_dict() for e in batch]
        resp = requests.post(f"{api_url}/events/ingest", json=payload, timeout=30)
        if resp.status_code == 200:
            result = resp.json()
            print(f"[emit] Ingested {result.get('accepted', 0)} events, "
                  f"{result.get('duplicates', 0)} duplicates, "
                  f"{result.get('rejected', 0)} rejected")
        else:
            print(f"[emit] API error {resp.status_code}: {resp.text[:200]}")
