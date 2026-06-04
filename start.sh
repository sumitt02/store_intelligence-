#!/bin/bash
# Seed DB if empty, then start API
cd /app

if [ ! -f /data/store_intelligence.db ]; then
  echo "[start] Seeding database with synthetic events..."
  PYTHONPATH=/app python pipeline/simulator.py /data/events.jsonl 2>/dev/null || true
  python - <<'EOF'
import json, sys, os
sys.path.insert(0, '/app')
os.environ['DB_PATH'] = '/data/store_intelligence.db'
from app.database import engine, Base
from app.ingestion import ingest_events
from sqlalchemy.orm import sessionmaker
Base.metadata.create_all(bind=engine)
Session = sessionmaker(bind=engine)
db = Session()
try:
    with open('/data/events.jsonl') as f:
        events = [json.loads(l) for l in f if l.strip()]
    for i in range(0, len(events), 500):
        ingest_events(events[i:i+500], db)
    print(f"[start] Seeded {len(events)} events")
except Exception as e:
    print(f"[start] Seed skipped: {e}")
finally:
    db.close()
EOF
fi

echo "[start] Starting API on port ${PORT:-8000}..."
exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
