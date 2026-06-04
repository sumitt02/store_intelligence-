"""
# PROMPT: Generate pytest fixtures for the Store Intelligence API.
# Use an in-memory SQLite database so tests are isolated and fast.
# Pre-populate with realistic event data covering happy path AND edge cases:
# empty store, all-staff clip, re-entry, billing queue abandon.
# CHANGES MADE: Added separate fixtures for each edge case scenario
# rather than one monolithic fixture — makes test failures easier to diagnose.
"""
import json
import uuid
from datetime import datetime, timezone, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.main import app
from app.models import EventORM

# Use file-based temp DB to avoid SQLite in-memory per-connection isolation
import tempfile, os

@pytest.fixture(scope="session")
def engine_fixture():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    url = f"sqlite:///{tmp.name}"
    eng = create_engine(url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=eng)
    yield eng
    Base.metadata.drop_all(bind=eng)
    eng.dispose()
    os.unlink(tmp.name)


@pytest.fixture()
def db_session(engine_fixture):
    Session = sessionmaker(bind=engine_fixture)
    session = Session()
    yield session
    session.rollback()
    # Wipe all events between tests
    from sqlalchemy import text as _text
    session.execute(_text("DELETE FROM events"))
    session.commit()
    session.close()


@pytest.fixture()
def client(db_session):
    def override_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ── Event factories ───────────────────────────────────────────────────────────

BASE_TIME = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
STORE_ID = "STORE_BLR_002"
CAMERA_ID = "CAM_ENTRY_01"


def _ts(delta_minutes: float = 0) -> str:
    return (BASE_TIME + timedelta(minutes=delta_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_event(
    event_type: str,
    visitor_id: str,
    zone_id: str | None = None,
    dwell_ms: int = 0,
    is_staff: bool = False,
    confidence: float = 0.90,
    delta_minutes: float = 0,
    queue_depth: int | None = None,
    store_id: str = STORE_ID,
) -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "store_id": store_id,
        "camera_id": CAMERA_ID,
        "visitor_id": visitor_id,
        "event_type": event_type,
        "timestamp": _ts(delta_minutes),
        "zone_id": zone_id,
        "dwell_ms": dwell_ms,
        "is_staff": is_staff,
        "confidence": confidence,
        "metadata": {
            "queue_depth": queue_depth,
            "sku_zone": zone_id if zone_id not in (None, "BILLING", "ENTRY_EXIT") else None,
            "session_seq": 1,
        },
    }


@pytest.fixture()
def happy_path_events() -> list[dict]:
    """Standard visitor flow: entry → zones → billing → exit."""
    vis = f"VIS_{uuid.uuid4().hex[:8]}"
    return [
        _make_event("ENTRY", vis, delta_minutes=0),
        _make_event("ZONE_ENTER", vis, "SKINCARE", delta_minutes=1),
        _make_event("ZONE_DWELL", vis, "SKINCARE", dwell_ms=45000, delta_minutes=2),
        _make_event("ZONE_EXIT", vis, "SKINCARE", dwell_ms=90000, delta_minutes=2.5),
        _make_event("ZONE_ENTER", vis, "BILLING", delta_minutes=3),
        _make_event("BILLING_QUEUE_JOIN", vis, "BILLING", queue_depth=2, delta_minutes=3.1),
        _make_event("ZONE_EXIT", vis, "BILLING", dwell_ms=120000, delta_minutes=5),
        _make_event("EXIT", vis, delta_minutes=5.5),
    ]


@pytest.fixture()
def staff_events() -> list[dict]:
    """Staff member visiting all zones quickly."""
    vis = f"VIS_{uuid.uuid4().hex[:8]}"
    events = [_make_event("ENTRY", vis, is_staff=True, delta_minutes=0)]
    for i, zone in enumerate(["SKINCARE", "HAIRCARE", "BILLING"]):
        events.append(_make_event("ZONE_ENTER", vis, zone, is_staff=True, delta_minutes=i + 0.5))
        events.append(_make_event("ZONE_EXIT", vis, zone, dwell_ms=30000, is_staff=True, delta_minutes=i + 1))
    events.append(_make_event("EXIT", vis, is_staff=True, delta_minutes=4))
    return events


@pytest.fixture()
def reentry_events() -> list[dict]:
    """Same visitor_id exits and re-enters → REENTRY event."""
    vis = f"VIS_{uuid.uuid4().hex[:8]}"
    return [
        _make_event("ENTRY", vis, delta_minutes=0),
        _make_event("ZONE_ENTER", vis, "SKINCARE", delta_minutes=1),
        _make_event("EXIT", vis, delta_minutes=3),
        _make_event("REENTRY", vis, delta_minutes=8),
        _make_event("ZONE_ENTER", vis, "BILLING", delta_minutes=9),
        _make_event("EXIT", vis, delta_minutes=12),
    ]


@pytest.fixture()
def abandon_events() -> list[dict]:
    """Visitor joins billing queue then abandons."""
    vis = f"VIS_{uuid.uuid4().hex[:8]}"
    return [
        _make_event("ENTRY", vis, delta_minutes=0),
        _make_event("ZONE_ENTER", vis, "BILLING", delta_minutes=2),
        _make_event("BILLING_QUEUE_JOIN", vis, "BILLING", queue_depth=5, delta_minutes=2.1),
        _make_event("BILLING_QUEUE_ABANDON", vis, "BILLING", dwell_ms=180000, delta_minutes=5),
        _make_event("EXIT", vis, delta_minutes=5.5),
    ]


@pytest.fixture()
def group_entry_events() -> list[dict]:
    """3 people enter together — should produce 3 ENTRY events."""
    return [
        _make_event("ENTRY", f"VIS_{uuid.uuid4().hex[:8]}", delta_minutes=0),
        _make_event("ENTRY", f"VIS_{uuid.uuid4().hex[:8]}", delta_minutes=0),
        _make_event("ENTRY", f"VIS_{uuid.uuid4().hex[:8]}", delta_minutes=0),
    ]
