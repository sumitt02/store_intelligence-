"""
Event ingestion: validate → deduplicate → store.

Idempotent: ingesting the same event_id twice is safe.
Partial success: malformed events are rejected with error details;
valid events in the same batch are still accepted.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import ValidationError
from sqlalchemy.orm import Session
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.models import StoreEvent, EventORM, IngestResponse

logger = logging.getLogger(__name__)


def ingest_events(raw_events: list[Any], db: Session) -> IngestResponse:
    accepted = 0
    duplicates = 0
    rejected = 0
    errors: list[dict] = []

    for idx, raw in enumerate(raw_events):
        if not isinstance(raw, dict):
            rejected += 1
            errors.append({"index": idx, "error": "Expected JSON object", "input": str(raw)[:100]})
            continue

        try:
            event = StoreEvent.model_validate(raw)
        except ValidationError as e:
            rejected += 1
            # Stringify error details — pydantic's error dicts can contain
            # non-JSON-serializable objects (ValueError instances etc.)
            safe_errors = [
                {"type": err.get("type"), "loc": list(err.get("loc", [])), "msg": err.get("msg")}
                for err in e.errors(include_url=False)
            ]
            errors.append({
                "index": idx,
                "event_id": raw.get("event_id", "unknown"),
                "error": safe_errors,
            })
            continue

        # Idempotent insert — on conflict (event_id already exists) do nothing
        stmt = sqlite_insert(EventORM).values(
            event_id=event.event_id,
            store_id=event.store_id,
            camera_id=event.camera_id,
            visitor_id=event.visitor_id,
            event_type=event.event_type,
            timestamp=event.timestamp,
            zone_id=event.zone_id,
            dwell_ms=event.dwell_ms,
            is_staff=event.is_staff,
            confidence=event.confidence,
            metadata_json=json.dumps(event.metadata.model_dump()),
        ).on_conflict_do_nothing(index_elements=["event_id"])

        result = db.execute(stmt)
        if result.rowcount == 0:
            duplicates += 1
        else:
            accepted += 1

    db.commit()

    logger.info(
        "ingest_complete: accepted=%d duplicates=%d rejected=%d",
        accepted, duplicates, rejected,
        extra={
            "accepted": accepted,
            "duplicates": duplicates,
            "rejected": rejected,
            "batch_size": len(raw_events),
        }
    )

    return IngestResponse(
        accepted=accepted,
        duplicates=duplicates,
        rejected=rejected,
        errors=errors,
    )
