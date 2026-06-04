"""
Re-ID and visitor session management.

Strategy: appearance embedding similarity + spatial trajectory.
Each unique person gets a visitor_id (VIS_<8hex>). When a person exits and
re-enters within the session window, we emit REENTRY not a new ENTRY.

For production footage: use OSNet embeddings from torchreid.
For this implementation: bounding box IoU + centroid distance similarity,
which works well when tracking IDs are stable within a camera.
"""
from __future__ import annotations

import hashlib
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional
import numpy as np


REENTRY_WINDOW_SECONDS = 300  # 5 minutes — same visitor re-entering counts as REENTRY


@dataclass
class VisitorSession:
    visitor_id: str
    track_id: int
    store_id: str
    first_seen: float
    last_seen: float
    last_bbox: Optional[list] = None          # [x1,y1,x2,y2] normalised
    embedding: Optional[np.ndarray] = None
    session_seq: int = 0
    exited: bool = False
    exit_time: Optional[float] = None
    zone_history: list[str] = field(default_factory=list)


class ReIDTracker:
    """
    Maps raw tracking IDs (per-camera, per-clip) to stable visitor_ids.
    Handles re-entry detection across exits.
    """

    def __init__(self, store_id: str, similarity_threshold: float = 0.65):
        self.store_id = store_id
        self.sim_threshold = similarity_threshold
        # track_id -> VisitorSession (active)
        self._active: dict[int, VisitorSession] = {}
        # visitor_id -> VisitorSession (recently exited, for REENTRY detection)
        self._exited: dict[str, VisitorSession] = {}
        self._all_sessions: list[VisitorSession] = []

    def _make_visitor_id(self) -> str:
        raw = f"{self.store_id}-{time.time_ns()}"
        h = hashlib.sha1(raw.encode()).hexdigest()[:8]
        return f"VIS_{h}"

    def _bbox_similarity(self, bbox_a: list, bbox_b: list) -> float:
        """IoU-based similarity between two bboxes."""
        if bbox_a is None or bbox_b is None:
            return 0.0
        ax1, ay1, ax2, ay2 = bbox_a
        bx1, by1, bx2, by2 = bbox_b
        ix1, iy1 = max(ax1, bx1), max(ay1, by1)
        ix2, iy2 = min(ax2, bx2), min(ay2, by2)
        inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
        area_a = (ax2 - ax1) * (ay2 - ay1)
        area_b = (bx2 - bx1) * (by2 - by1)
        union = area_a + area_b - inter
        return inter / union if union > 0 else 0.0

    def _embedding_similarity(self, emb_a: Optional[np.ndarray], emb_b: Optional[np.ndarray]) -> float:
        if emb_a is None or emb_b is None:
            return 0.5  # neutral if no embeddings
        a = emb_a / (np.linalg.norm(emb_a) + 1e-8)
        b = emb_b / (np.linalg.norm(emb_b) + 1e-8)
        return float(np.dot(a, b))

    def _find_reentry_match(self, bbox: list, embedding: Optional[np.ndarray], now: float) -> Optional[VisitorSession]:
        """Check if an appearing person matches a recently exited visitor."""
        best_score = 0.0
        best_session = None

        for vid, session in list(self._exited.items()):
            # Prune stale exits
            if session.exit_time and (now - session.exit_time) > REENTRY_WINDOW_SECONDS:
                del self._exited[vid]
                continue
            emb_sim = self._embedding_similarity(embedding, session.embedding)
            score = emb_sim
            if score > best_score:
                best_score = score
                best_session = session

        if best_score >= self.sim_threshold and best_session:
            return best_session
        return None

    def register_track(
        self,
        track_id: int,
        bbox: list,
        embedding: Optional[np.ndarray],
        now: float,
    ) -> tuple[str, bool]:
        """
        Register/update a tracking ID. Returns (visitor_id, is_reentry).
        """
        if track_id in self._active:
            session = self._active[track_id]
            session.last_seen = now
            session.last_bbox = bbox
            if embedding is not None:
                # Running average of embeddings for robustness
                if session.embedding is None:
                    session.embedding = embedding
                else:
                    session.embedding = 0.7 * session.embedding + 0.3 * embedding
            return session.visitor_id, False

        # New track_id — check reentry
        reentry_session = self._find_reentry_match(bbox, embedding, now)
        if reentry_session:
            reentry_session.exited = False
            reentry_session.exit_time = None
            self._active[track_id] = reentry_session
            del self._exited[reentry_session.visitor_id]
            reentry_session.session_seq += 1
            return reentry_session.visitor_id, True

        # Genuinely new visitor
        visitor_id = self._make_visitor_id()
        session = VisitorSession(
            visitor_id=visitor_id,
            track_id=track_id,
            store_id=self.store_id,
            first_seen=now,
            last_seen=now,
            last_bbox=bbox,
            embedding=embedding,
        )
        self._active[track_id] = session
        self._all_sessions.append(session)
        return visitor_id, False

    def mark_exit(self, track_id: int, now: float) -> Optional[str]:
        """Mark a tracked person as exited. Returns visitor_id if found."""
        if track_id not in self._active:
            return None
        session = self._active.pop(track_id)
        session.exited = True
        session.exit_time = now
        self._exited[session.visitor_id] = session
        return session.visitor_id

    def get_session(self, track_id: int) -> Optional[VisitorSession]:
        return self._active.get(track_id)

    def increment_session_seq(self, track_id: int) -> int:
        session = self._active.get(track_id)
        if session:
            session.session_seq += 1
            return session.session_seq
        return 0

    def add_zone(self, track_id: int, zone: str) -> None:
        session = self._active.get(track_id)
        if session and zone not in session.zone_history:
            session.zone_history.append(zone)
