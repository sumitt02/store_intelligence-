"""
Store Intelligence API — FastAPI entrypoint.

Endpoints:
  POST /events/ingest              — batch ingest, idempotent by event_id
  GET  /stores/{store_id}/metrics  — real-time store KPIs
  GET  /stores/{store_id}/funnel   — conversion funnel
  GET  /stores/{store_id}/heatmap  — zone heatmap (normalised 0-100)
  GET  /stores/{store_id}/anomalies — active operational anomalies
  GET  /health                     — per-store feed freshness

Production features:
  - Structured JSON logging with trace_id on every request
  - Graceful degradation: DB unavailable → HTTP 503 with structured body
  - Idempotent ingest: POST /events/ingest safe to call twice with same payload
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Depends, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.database import engine, Base, get_db
from app.ingestion import ingest_events
from app.metrics import compute_metrics
from app.funnel import compute_funnel
from app.heatmap import compute_heatmap
from app.anomalies import detect_anomalies
from app.health import get_health
from app.revenue import compute_revenue
from app.journey import compute_journey
from app.insights import generate_insights
from app.models import IngestResponse, StoreMetrics, StoreFunnel, StoreHeatmap, StoreAnomalies, HealthResponse
from app.logging_config import setup_logging, RequestLoggingMiddleware

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
    Base.metadata.create_all(bind=engine)
    logger.info("Store Intelligence API started", extra={"event": "startup"})
    yield
    logger.info("Store Intelligence API stopping", extra={"event": "shutdown"})


app = FastAPI(
    title="Store Intelligence API",
    description="Real-time retail analytics from CCTV detection pipeline",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RequestLoggingMiddleware)


def db_error_response(exc: Exception) -> JSONResponse:
    """Structured 503 — no raw stack traces exposed."""
    logger.error("database_error", extra={"error": str(exc)})
    return JSONResponse(
        status_code=503,
        content={
            "error": "SERVICE_UNAVAILABLE",
            "detail": "Database temporarily unavailable. Please retry.",
            "retry_after_seconds": 5,
        }
    )


@app.post(
    "/events/ingest",
    response_model=IngestResponse,
    summary="Batch ingest events",
    description="Accepts up to 500 events. Idempotent by event_id. Partial success on malformed events.",
)
async def post_ingest(
    request: Request,
    db: Session = Depends(get_db),
) -> IngestResponse:
    try:
        body: list[Any] = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Request body must be a JSON array of events")

    if not isinstance(body, list):
        raise HTTPException(status_code=422, detail="Request body must be a JSON array")

    if len(body) > 500:
        raise HTTPException(status_code=413, detail=f"Batch too large: {len(body)} events (max 500)")

    try:
        result = ingest_events(body, db)
    except OperationalError as e:
        return db_error_response(e)

    # Log event count for structured logging
    logger.info(
        "ingest complete",
        extra={
            "trace_id": getattr(request.state, "trace_id", ""),
            "event_count": len(body),
            "accepted": result.accepted,
            "duplicates": result.duplicates,
            "rejected": result.rejected,
        }
    )
    return result


@app.get(
    "/stores/{store_id}/metrics",
    response_model=StoreMetrics,
    summary="Real-time store metrics",
)
async def get_metrics(
    store_id: str,
    db: Session = Depends(get_db),
    date: str = Query(None, description="Filter to a specific date (YYYY-MM-DD), defaults to today"),
) -> StoreMetrics:
    try:
        return compute_metrics(store_id, db, date=date)
    except OperationalError as e:
        return db_error_response(e)


@app.get(
    "/stores/{store_id}/funnel",
    response_model=StoreFunnel,
    summary="Conversion funnel",
)
async def get_funnel(
    store_id: str,
    db: Session = Depends(get_db),
    date: str = Query(None, description="Filter to a specific date (YYYY-MM-DD), defaults to today"),
) -> StoreFunnel:
    try:
        return compute_funnel(store_id, db, date=date)
    except OperationalError as e:
        return db_error_response(e)


@app.get(
    "/stores/{store_id}/heatmap",
    response_model=StoreHeatmap,
    summary="Zone heatmap (normalised 0-100)",
)
async def get_heatmap(
    store_id: str,
    db: Session = Depends(get_db),
    date: str = Query(None, description="Filter to a specific date (YYYY-MM-DD), defaults to today"),
) -> StoreHeatmap:
    try:
        return compute_heatmap(store_id, db, date=date)
    except OperationalError as e:
        return db_error_response(e)


@app.get(
    "/stores/{store_id}/anomalies",
    response_model=StoreAnomalies,
    summary="Active operational anomalies",
)
async def get_anomalies(store_id: str, db: Session = Depends(get_db)) -> StoreAnomalies:
    try:
        return detect_anomalies(store_id, db)
    except OperationalError as e:
        return db_error_response(e)


@app.get(
    "/health",
    response_model=HealthResponse,
    summary="Service health + per-store feed freshness",
)
async def get_health_endpoint(db: Session = Depends(get_db)) -> HealthResponse:
    try:
        return get_health(db)
    except OperationalError as e:
        return db_error_response(e)


@app.get(
    "/stores/{store_id}/revenue",
    summary="Revenue intelligence — POS analytics tied to footfall",
)
async def get_revenue(
    store_id: str,
    db: Session = Depends(get_db),
    date: str = Query(None, description="Date (YYYY-MM-DD), defaults to today"),
):
    try:
        return compute_revenue(store_id, db, date=date)
    except OperationalError as e:
        return db_error_response(e)


@app.get(
    "/stores/{store_id}/journey",
    summary="Customer journey analytics — zone path sequences and brand affinity",
)
async def get_journey(
    store_id: str,
    db: Session = Depends(get_db),
    date: str = Query(None, description="Date (YYYY-MM-DD), defaults to today"),
):
    try:
        return compute_journey(store_id, db, date=date)
    except OperationalError as e:
        return db_error_response(e)


@app.get(
    "/stores/{store_id}/insights",
    summary="AI-generated actionable store insights and recommendations",
)
async def get_insights(
    store_id: str,
    db: Session = Depends(get_db),
    date: str = Query(None, description="Date (YYYY-MM-DD), defaults to today"),
):
    try:
        return generate_insights(store_id, db, date=date)
    except OperationalError as e:
        return db_error_response(e)


@app.get("/dashboard", include_in_schema=False)
async def dashboard():
    """Serve the live dashboard."""
    from pathlib import Path
    dashboard_path = Path(__file__).parent.parent / "dashboard" / "index.html"
    if dashboard_path.exists():
        return FileResponse(str(dashboard_path), media_type="text/html")
    return {"error": "Dashboard not found"}


@app.get("/", include_in_schema=False)
async def root():
    return {"service": "Store Intelligence API", "version": "1.0.0", "docs": "/docs", "dashboard": "/dashboard"}
