"""Long-footage segmentation — vendored from tennis-coach src/segment.py.

Changes for the service: package-relative imports; extract_pose imported lazily
(so the grader can run on cached arrays without mediapipe installed); the audio
onset path is GONE — wrist-speed peaks only propose candidate swings, and
pipeline.ballgate verifies/refines each contact from the ball's trajectory
(pure vision; outdoor clips often have no audible ball "pock" anyway).
"""
from pathlib import Path

import numpy as np
from scipy.signal import find_peaks

from . import config


def wrist_speed_series(data: dict) -> np.ndarray:
    """Per-frame dominant-wrist speed in normalized-image units/sec (0 where no pose)."""
    wrist_idx = config.LM[f"{config.DOMINANT_HAND}_wrist"]
    fps = data["fps"]
    norm = data["norm"]  # [N, 33, 4]
    if norm.shape[0] == 0:
        return np.zeros(0)
    xs = norm[:, wrist_idx, 0]
    ys = norm[:, wrist_idx, 1]
    dx = np.diff(xs, prepend=xs[0])
    dy = np.diff(ys, prepend=ys[0])
    speed = np.sqrt(dx ** 2 + dy ** 2) * fps
    return np.nan_to_num(speed, nan=0.0)


def detect_strokes(video_path: Path, data: dict | None = None) -> list[dict]:
    """Candidate swings from dominant-wrist speed peaks (chronological).

    These are CANDIDATES only — nothing here knows whether a ball was struck.
    Run pipeline.ballgate.verify_hits() on the result to confirm real hits and
    re-anchor contact_frame on the observed ball direction change.
    """
    video_path = Path(video_path)
    if data is None:
        from . import extract_pose
        data = extract_pose.load(video_path)
    fps = data["fps"]
    n = data["n_frames"]

    speed = wrist_speed_series(data)
    moving = speed[speed > 0]
    if moving.size == 0:
        print("[segment] no pose motion found in clip")
        return []
    height = np.percentile(moving, config.STROKE_SPEED_PERCENTILE)
    distance = max(1, int(config.STROKE_MIN_GAP_SEC * fps))
    peaks, _ = find_peaks(speed, height=height, distance=distance)

    win = int(config.STROKE_WINDOW_SEC * fps)
    strokes = []
    for i, peak in enumerate(peaks):
        contact = int(peak)
        strokes.append({
            "stroke_no": i + 1,
            "peak_frame": int(peak),
            "contact_frame": contact,
            "time_s": round(contact / fps, 2),
            "contact_from_ball": False,   # ballgate.verify_hits fills this in
            "window": (max(0, contact - win), min(n - 1, contact + win)),
        })

    print(f"[segment] {len(strokes)} candidate swings detected "
          f"(speed>{height:.2f}, min gap {config.STROKE_MIN_GAP_SEC}s)")
    return strokes
