"""
Pydantic schemas (API contracts) + SQLAlchemy ORM models (storage).
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Optional, Any

from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import Column, String, Integer, Boolean, Float, Text, Index, DateTime
from sqlalchemy.sql import func

from app.database import Base


# ── SQLAlchemy ORM ──────────────────────────────────────────────────────────

class EventORM(Base):
    __tablename__ = "events"

    event_id = Column(String, primary_key=True)
    store_id = Column(String, nullable=False, index=True)
    camera_id = Column(String, nullable=False)
    visitor_id = Column(String, nullable=False, index=True)
    event_type = Column(String, nullable=False, index=True)
    timestamp = Column(String, nullable=False, index=True)
    zone_id = Column(String, nullable=True)
    dwell_ms = Column(Integer, default=0)
    is_staff = Column(Boolean, default=False)
    confidence = Column(Float, default=1.0)
    metadata_json = Column(Text, default="{}")  # serialised EventMetadata
    ingested_at = Column(String, default=lambda: datetime.utcnow().isoformat() + "Z")

    __table_args__ = (
        Index("ix_events_store_timestamp", "store_id", "timestamp"),
        Index("ix_events_visitor_store", "visitor_id", "store_id"),
    )


class POSTransactionORM(Base):
    __tablename__ = "pos_transactions"

    transaction_id = Column(String, primary_key=True)
    store_id = Column(String, nullable=False, index=True)
    timestamp = Column(String, nullable=False, index=True)
    basket_value_inr = Column(Float, nullable=False)


# ── Pydantic schemas ─────────────────────────────────────────────────────────

VALID_EVENT_TYPES = {
    "ENTRY", "EXIT", "ZONE_ENTER", "ZONE_EXIT",
    "ZONE_DWELL", "BILLING_QUEUE_JOIN", "BILLING_QUEUE_ABANDON", "REENTRY"
}


class EventMetadata(BaseModel):
    queue_depth: Optional[int] = None
    sku_zone: Optional[str] = None
    session_seq: int = 0


class StoreEvent(BaseModel):
    event_id: str = Field(..., description="UUID-v4, globally unique")
    store_id: str
    camera_id: str
    visitor_id: str
    event_type: str
    timestamp: str = Field(..., description="ISO-8601 UTC")
    zone_id: Optional[str] = None
    dwell_ms: int = 0
    is_staff: bool = False
    confidence: float = Field(..., ge=0.0, le=1.0)
    metadata: EventMetadata = Field(default_factory=EventMetadata)

    @field_validator("event_type")
    @classmethod
    def valid_event_type(cls, v: str) -> str:
        if v not in VALID_EVENT_TYPES:
            raise ValueError(f"Unknown event_type '{v}'. Valid: {VALID_EVENT_TYPES}")
        return v

    @field_validator("timestamp")
    @classmethod
    def valid_timestamp(cls, v: str) -> str:
        try:
            datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(f"timestamp must be ISO-8601 UTC, got: {v!r}")
        return v

    @model_validator(mode="after")
    def zone_required_for_zone_events(self) -> "StoreEvent":
        zone_events = {"ZONE_ENTER", "ZONE_EXIT", "ZONE_DWELL",
                       "BILLING_QUEUE_JOIN", "BILLING_QUEUE_ABANDON"}
        if self.event_type in zone_events and not self.zone_id:
            raise ValueError(f"zone_id is required for event_type={self.event_type}")
        return self


class IngestResponse(BaseModel):
    accepted: int
    duplicates: int
    rejected: int
    errors: list[dict] = Field(default_factory=list)


class ZoneMetric(BaseModel):
    zone_id: str
    avg_dwell_ms: float
    visit_count: int


class StoreMetrics(BaseModel):
    store_id: str
    as_of: str
    unique_visitors: int
    conversion_rate: float
    avg_dwell_per_zone: list[ZoneMetric]
    queue_depth: int
    abandonment_rate: float


class FunnelStage(BaseModel):
    stage: str
    count: int
    drop_off_pct: float


class StoreFunnel(BaseModel):
    store_id: str
    as_of: str
    stages: list[FunnelStage]


class HeatmapZone(BaseModel):
    zone_id: str
    visit_frequency: int
    avg_dwell_ms: float
    score: float = Field(..., description="Normalised 0-100")
    data_confidence: bool = True


class StoreHeatmap(BaseModel):
    store_id: str
    as_of: str
    zones: list[HeatmapZone]


class Anomaly(BaseModel):
    anomaly_id: str
    store_id: str
    anomaly_type: str
    severity: str  # INFO / WARN / CRITICAL
    detected_at: str
    description: str
    suggested_action: str
    metadata: dict = Field(default_factory=dict)


class StoreAnomalies(BaseModel):
    store_id: str
    as_of: str
    anomalies: list[Anomaly]


class StoreHealth(BaseModel):
    store_id: str
    status: str  # OK / STALE_FEED / NO_DATA
    last_event_timestamp: Optional[str]
    lag_minutes: Optional[float]


class HealthResponse(BaseModel):
    status: str
    checked_at: str
    stores: list[StoreHealth]
