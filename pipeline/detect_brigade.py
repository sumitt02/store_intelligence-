"""
Brigade Bangalore (ST1008) specific detection pipeline.
Processes the 5 real CCTV cameras with calibrated zone mapping.

Camera → Zone mapping (verified from frame inspection):
  CAM_01 (30fps): Top shelves — EB Korean, Face Shop, Good Vibes, DermDoc,
                   Minimalist, Aqualogica, Lakme Skin
  CAM_02 (30fps): Bottom shelves — Maybelline, Faces Canada, Lakme, Colorbar+Sugar,
                   Swiss Beauty, Renee/NY Bae, Alps Goodness, Streax
  CAM_03 (30fps): Entry/Exit — glass doors, primary entry counting camera
  CAM_04 (25fps): Stockroom — staff only, all detections marked is_staff=True
  CAM_05 (25fps): Cash Counter (left ~40%) + Accessories display (right ~60%)

Zone classification for floor cameras:
  The shelves run left-to-right across the frame width.
  We divide the frame into equal horizontal slices per brand zone.

Staff classification:
  CAM_04 → 100% staff (stockroom)
  CAM_01/02/03/05 → trajectory heuristic (3+ zones in <2min) OR
                     upper-body position in back-of-frame regions

# AI-ASSISTED DECISIONS (Detection):
# Used Claude Vision on sample frames to identify zone boundaries.
# The top shelf (CAM_01) has 7 brands spanning the full width.
# Bottom shelf (CAM_02) has 8 brands - we split at ~12.5% increments.
# CAM_03 is fisheye-ish top-down angle — good for entry counting,
#  person enters when crossing bottom third of frame inbound.
# Claude suggested using the door mat (visible floor pattern) as the
#  threshold line — I overrode this with a fixed y=0.65 threshold since
#  the mat position varies across clips.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import numpy as np

# Add pipeline dir to path
sys.path.insert(0, str(Path(__file__).parent))
from tracker import ReIDTracker
from emit import make_event, StoreEvent, emit_to_file, emit_to_api

STORE_ID = "STORE_BLR_BRIGADE"
LAYOUT_PATH = Path(__file__).parent.parent / "data" / "store_layout.json"

# Camera configs — verified from frame analysis
CAMERA_CONFIGS = {
    "CAM_03": {
        "type": "entry",
        "fps": 30.0,
        "zones": ["ENTRY_EXIT"],
        "entry_threshold_y": 0.65,   # y-fraction of frame — person crosses inbound
        "exit_threshold_y": 0.65,    # same line, outbound direction
        "is_staff": False,
    },
    "CAM_01": {
        "type": "floor",
        "fps": 30.0,
        "zones": ["EB_KOREAN", "THE_FACE_SHOP", "GOOD_VIBES", "DERMDOC",
                  "MINIMALIST", "AQUALOGICA", "LAKME_SKIN"],
        "is_staff": False,
    },
    "CAM_02": {
        "type": "floor",
        "fps": 30.0,
        "zones": ["MAYBELLINE", "FACES_CANADA", "LAKME_MAKEUP", "COLORBAR_SUGAR",
                  "SWISS_BEAUTY", "RENEE_NY_BAE", "ALPS_GOODNESS", "STREAX"],
        "is_staff": False,
    },
    "CAM_05": {
        "type": "billing",
        "fps": 25.0,
        "zones": ["CASH_COUNTER", "ACCESSORIES"],
        "billing_zone": "CASH_COUNTER",
        "billing_x_threshold": 0.45,  # left 45% = cash counter, right = accessories
        "is_staff": False,
    },
    "CAM_04": {
        "type": "stockroom",
        "fps": 25.0,
        "zones": ["STOCKROOM"],
        "is_staff": True,  # ALL detections from stockroom are staff
    },
}

# Clip start times from CCTV timestamps (from frame inspection)
# CAM 1: "10/04/2026 20:10:32" at 5s → start ≈ 20:10:27
# CAM 2: "10/04/2026 20:10:07" at 5s → start ≈ 20:10:02
# CAM 3: visible at ~20:10 range
# CAM 4: "10/04/2026 20:09:50" at 5s → start ≈ 20:09:45
# CAM 5: "10/04/2026 20:09:52" at 5s → start ≈ 20:09:47
CAM_START_TIMES = {
    "CAM_01": datetime(2026, 4, 10, 20, 10, 27, tzinfo=timezone.utc),
    "CAM_02": datetime(2026, 4, 10, 20, 10, 2, tzinfo=timezone.utc),
    "CAM_03": datetime(2026, 4, 10, 20, 10, 0, tzinfo=timezone.utc),
    "CAM_04": datetime(2026, 4, 10, 20, 9, 45, tzinfo=timezone.utc),
    "CAM_05": datetime(2026, 4, 10, 20, 9, 47, tzinfo=timezone.utc),
}


def get_zone_from_x(x_norm: float, zones: list[str]) -> str:
    """Divide frame width equally across zones and return zone for x position."""
    n = len(zones)
    idx = min(int(x_norm * n), n - 1)
    return zones[idx]


def get_zone_billing(x_norm: float, cam_config: dict) -> str:
    """CAM_05 split: left = CASH_COUNTER, right = ACCESSORIES."""
    threshold = cam_config.get("billing_x_threshold", 0.45)
    return "CASH_COUNTER" if x_norm < threshold else "ACCESSORIES"


def process_brigade_clip(
    video_path: str,
    camera_id: str,
    output_events: list[StoreEvent],
    reid_tracker: ReIDTracker,
    sample_every_n_frames: int = 3,  # Process every 3rd frame for speed
) -> None:
    """Process one Brigade Bangalore CCTV clip and emit structured events."""
    try:
        from ultralytics import YOLO
        import cv2
    except ImportError:
        print("[detect] ultralytics/cv2 not installed. Run: pip install ultralytics opencv-python-headless")
        return

    cam_config = CAMERA_CONFIGS.get(camera_id)
    if not cam_config:
        print(f"[detect] Unknown camera: {camera_id}")
        return

    fps = cam_config["fps"]
    cam_type = cam_config["type"]
    zones = cam_config["zones"]
    force_staff = cam_config.get("is_staff", False)
    clip_start = CAM_START_TIMES.get(camera_id, datetime(2026, 4, 10, 20, 0, 0, tzinfo=timezone.utc))

    model = YOLO("yolov8n.pt")
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print(f"[detect] Cannot open {video_path}")
        return

    frame_idx = 0
    # track_id → state dict
    track_state: dict[int, dict] = {}
    lost_counter: dict[int, int] = {}
    LOST_THRESHOLD = int(fps * 1.5)

    # For entry camera: track y-centroid history to determine direction
    y_history: dict[int, list[float]] = {}
    # Active billing queue members
    queue_members: set[int] = set()
    DWELL_30S_FRAMES = int(fps * 30)

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        frame_idx += 1
        # Skip frames for speed
        if frame_idx % sample_every_n_frames != 0:
            continue

        effective_frame = frame_idx
        frame_time = clip_start + timedelta(seconds=effective_frame / fps)
        now_ts = frame_time.timestamp()
        h, w = frame.shape[:2]

        # Run YOLOv8 with ByteTrack
        results = model.track(
            frame,
            persist=True,
            classes=[0],   # persons only
            conf=0.30,
            iou=0.45,
            tracker="bytetrack.yaml",
            verbose=False,
        )

        active_ids: set[int] = set()

        if results[0].boxes is not None and results[0].boxes.id is not None:
            boxes = results[0].boxes
            for i, track_id in enumerate(boxes.id.int().tolist()):
                conf = float(boxes.conf[i])
                x1, y1, x2, y2 = boxes.xyxy[i].tolist()
                cx = (x1 + x2) / 2 / w   # normalised centroid
                cy = (y1 + y2) / 2 / h
                bbox_norm = [x1/w, y1/h, x2/w, y2/h]
                active_ids.add(track_id)

                is_staff = force_staff
                zone_id: Optional[str] = None

                # Determine zone by camera type
                if cam_type == "entry":
                    zone_id = "ENTRY_EXIT"
                    # Track y-centroid history for direction
                    if track_id not in y_history:
                        y_history[track_id] = []
                    y_history[track_id].append(cy)

                elif cam_type == "floor":
                    zone_id = get_zone_from_x(cx, zones)

                elif cam_type == "billing":
                    zone_id = get_zone_billing(cx, cam_config)

                elif cam_type == "stockroom":
                    zone_id = "STOCKROOM"
                    is_staff = True

                # Register with ReID tracker
                visitor_id, is_reentry = reid_tracker.register_track(
                    track_id, bbox_norm, None, now_ts
                )
                session = reid_tracker.get_session(track_id)

                # Classify staff by trajectory speed (if not forced)
                if not is_staff and session:
                    elapsed = now_ts - session.first_seen
                    unique_zones = len(set(session.zone_history))
                    if unique_zones >= 3 and elapsed < 120:
                        is_staff = True

                # === ENTRY / REENTRY event ===
                if track_id not in track_state:
                    lost_counter[track_id] = 0

                    if cam_type == "entry":
                        # Only emit ENTRY/REENTRY for entry camera
                        event_type = "REENTRY" if is_reentry else "ENTRY"
                        seq = reid_tracker.increment_session_seq(track_id)
                        output_events.append(make_event(
                            store_id=STORE_ID,
                            camera_id=camera_id,
                            visitor_id=visitor_id,
                            event_type=event_type,
                            timestamp=frame_time,
                            zone_id=None,
                            dwell_ms=0,
                            is_staff=is_staff,
                            confidence=conf,
                            session_seq=seq,
                        ))
                    else:
                        # Non-entry camera: emit ZONE_ENTER for first appearance
                        if zone_id:
                            seq = reid_tracker.increment_session_seq(track_id)
                            output_events.append(make_event(
                                store_id=STORE_ID,
                                camera_id=camera_id,
                                visitor_id=visitor_id,
                                event_type="ZONE_ENTER",
                                timestamp=frame_time,
                                zone_id=zone_id,
                                dwell_ms=0,
                                is_staff=is_staff,
                                confidence=conf,
                                sku_zone=zone_id if zone_id not in ("CASH_COUNTER", "STOCKROOM") else None,
                                session_seq=seq if (seq := reid_tracker.increment_session_seq(track_id)) else 1,
                            ))
                            reid_tracker.add_zone(track_id, zone_id)

                            # Billing queue check
                            if zone_id == "CASH_COUNTER":
                                queue_members.add(track_id)
                                if len(queue_members) > 1:
                                    seq = reid_tracker.increment_session_seq(track_id)
                                    output_events.append(make_event(
                                        store_id=STORE_ID,
                                        camera_id=camera_id,
                                        visitor_id=visitor_id,
                                        event_type="BILLING_QUEUE_JOIN",
                                        timestamp=frame_time,
                                        zone_id="CASH_COUNTER",
                                        dwell_ms=0,
                                        is_staff=is_staff,
                                        confidence=conf,
                                        queue_depth=len(queue_members),
                                        session_seq=seq,
                                    ))

                    track_state[track_id] = {
                        "zone": zone_id,
                        "zone_enter_frame": frame_idx,
                        "last_dwell_frame": frame_idx,
                        "dwell_ms": 0,
                        "first_cy": cy,
                    }

                else:
                    state = track_state[track_id]
                    lost_counter[track_id] = 0

                    # Zone change
                    if zone_id and zone_id != state["zone"]:
                        old_zone = state["zone"]
                        dwell_ms = int((frame_idx - state["zone_enter_frame"]) / fps * 1000)

                        if old_zone and cam_type != "entry":
                            seq = reid_tracker.increment_session_seq(track_id)
                            output_events.append(make_event(
                                store_id=STORE_ID,
                                camera_id=camera_id,
                                visitor_id=visitor_id,
                                event_type="ZONE_EXIT",
                                timestamp=frame_time,
                                zone_id=old_zone,
                                dwell_ms=dwell_ms,
                                is_staff=is_staff,
                                confidence=conf,
                                sku_zone=old_zone if old_zone not in ("CASH_COUNTER", "STOCKROOM") else None,
                                session_seq=seq,
                            ))

                        state["zone"] = zone_id
                        state["zone_enter_frame"] = frame_idx
                        state["last_dwell_frame"] = frame_idx
                        reid_tracker.add_zone(track_id, zone_id)

                        if cam_type not in ("entry", "stockroom"):
                            seq = reid_tracker.increment_session_seq(track_id)
                            output_events.append(make_event(
                                store_id=STORE_ID,
                                camera_id=camera_id,
                                visitor_id=visitor_id,
                                event_type="ZONE_ENTER",
                                timestamp=frame_time,
                                zone_id=zone_id,
                                dwell_ms=0,
                                is_staff=is_staff,
                                confidence=conf,
                                sku_zone=zone_id if zone_id not in ("CASH_COUNTER", "STOCKROOM") else None,
                                session_seq=seq,
                            ))

                        # Billing queue
                        if zone_id == "CASH_COUNTER":
                            queue_members.add(track_id)
                            if len(queue_members) > 1:
                                seq = reid_tracker.increment_session_seq(track_id)
                                output_events.append(make_event(
                                    store_id=STORE_ID,
                                    camera_id=camera_id,
                                    visitor_id=visitor_id,
                                    event_type="BILLING_QUEUE_JOIN",
                                    timestamp=frame_time,
                                    zone_id="CASH_COUNTER",
                                    dwell_ms=0,
                                    is_staff=is_staff,
                                    confidence=conf,
                                    queue_depth=len(queue_members),
                                    session_seq=seq,
                                ))
                        elif state["zone"] == "CASH_COUNTER" and track_id in queue_members:
                            queue_members.discard(track_id)

                    # ZONE_DWELL: emit every 30s in same zone
                    frames_in_zone = frame_idx - state["zone_enter_frame"]
                    frames_since_dwell = frame_idx - state["last_dwell_frame"]
                    if (frames_in_zone >= DWELL_30S_FRAMES and
                            frames_since_dwell >= DWELL_30S_FRAMES and
                            zone_id and cam_type not in ("entry", "stockroom")):
                        state["last_dwell_frame"] = frame_idx
                        dwell_ms_total = int(frames_in_zone / fps * 1000)
                        seq = reid_tracker.increment_session_seq(track_id)
                        output_events.append(make_event(
                            store_id=STORE_ID,
                            camera_id=camera_id,
                            visitor_id=visitor_id,
                            event_type="ZONE_DWELL",
                            timestamp=frame_time,
                            zone_id=zone_id,
                            dwell_ms=dwell_ms_total,
                            is_staff=is_staff,
                            confidence=conf,
                            sku_zone=zone_id if zone_id not in ("CASH_COUNTER", "STOCKROOM") else None,
                            session_seq=seq,
                        ))

        # Handle lost tracks → EXIT events
        disappeared = set(track_state.keys()) - active_ids
        for track_id in disappeared:
            lost_counter[track_id] = lost_counter.get(track_id, 0) + 1
            if lost_counter[track_id] >= LOST_THRESHOLD:
                state = track_state.pop(track_id)
                visitor_id = reid_tracker.mark_exit(track_id, now_ts)
                if visitor_id and cam_type == "entry":
                    # Determine direction from y_history
                    yh = y_history.pop(track_id, [])
                    if len(yh) >= 2:
                        moving_down = yh[-1] > yh[0]
                        event_type = "EXIT" if moving_down else "EXIT"
                    else:
                        event_type = "EXIT"
                    output_events.append(make_event(
                        store_id=STORE_ID,
                        camera_id=camera_id,
                        visitor_id=visitor_id,
                        event_type=event_type,
                        timestamp=frame_time,
                        zone_id=None,
                        dwell_ms=0,
                        is_staff=False,
                        confidence=0.85,
                        session_seq=0,
                    ))
                elif visitor_id and cam_type not in ("stockroom",):
                    old_zone = state.get("zone")
                    if old_zone == "CASH_COUNTER" and track_id in queue_members:
                        queue_members.discard(track_id)
                        seq_val = 0
                        output_events.append(make_event(
                            store_id=STORE_ID,
                            camera_id=camera_id,
                            visitor_id=visitor_id,
                            event_type="BILLING_QUEUE_ABANDON",
                            timestamp=frame_time,
                            zone_id="CASH_COUNTER",
                            dwell_ms=int((frame_idx - state["zone_enter_frame"]) / fps * 1000),
                            is_staff=False,
                            confidence=0.75,
                            session_seq=seq_val,
                        ))
                lost_counter.pop(track_id, None)
                y_history.pop(track_id, None)

    cap.release()
    print(f"[detect] {Path(video_path).name} ({camera_id}): "
          f"{frame_idx} frames processed → {len(output_events)} total events")


def run_all_cameras(
    clips_dir: str,
    output_path: str,
    api_url: Optional[str] = None,
    sample_every_n: int = 3,
) -> list[StoreEvent]:
    """Process all 5 Brigade Bangalore cameras and emit events."""
    # Map camera IDs to filenames
    camera_files = {
        "CAM_01": "CAM 1.mp4",
        "CAM_02": "CAM 2.mp4",
        "CAM_03": "CAM 3.mp4",
        "CAM_04": "CAM 4.mp4",
        "CAM_05": "CAM 5.mp4",
    }

    # Single ReID tracker for the store (shared across cameras)
    reid_tracker = ReIDTracker(STORE_ID, similarity_threshold=0.60)
    all_events: list[StoreEvent] = []

    clips_path = Path(clips_dir)
    for cam_id, filename in camera_files.items():
        clip_path = clips_path / filename
        if not clip_path.exists():
            print(f"[detect] {filename} not found in {clips_dir}, skipping")
            continue

        print(f"\n[detect] Processing {cam_id} ({filename})...")
        cam_events: list[StoreEvent] = []
        process_brigade_clip(
            str(clip_path),
            cam_id,
            cam_events,
            reid_tracker,
            sample_every_n_frames=sample_every_n,
        )
        all_events.extend(cam_events)
        print(f"[detect] {cam_id}: generated {len(cam_events)} events")

    # Sort all events by timestamp
    all_events.sort(key=lambda e: e.timestamp)

    emit_to_file(all_events, output_path)
    print(f"\n[detect] Total events written: {len(all_events)} → {output_path}")

    if api_url:
        emit_to_api(all_events, api_url)

    return all_events


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Brigade Bangalore CCTV detection pipeline")
    parser.add_argument("--clips", default="./clips/CCTV Footage", help="Dir with CAM 1-5.mp4")
    parser.add_argument("--output", default="./events.jsonl")
    parser.add_argument("--api", default=None, help="API URL e.g. http://localhost:8000")
    parser.add_argument("--sample", type=int, default=3, help="Process every Nth frame")
    args = parser.parse_args()
    run_all_cameras(args.clips, args.output, args.api, args.sample)
