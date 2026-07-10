"""Long-footage segmentation — vendored from tennis-coach src/segment.py.

Changes for the service: package-relative imports; extract_pose imported lazily
(so the grader can run on cached arrays without mediapipe installed); audio onsets
prefer a sibling <stem>.wav (the worker writes one with ffmpeg — soundfile reads it
natively, no mp4 audio decoding needed).
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


def _audio_onset_times(video_path: Path) -> np.ndarray:
    try:
        import librosa
        wav = video_path.with_suffix(".wav")
        src = wav if wav.exists() else video_path
        y, sr = librosa.load(str(src), sr=None, mono=True)
        if y.size == 0:
            return np.array([])
        return librosa.onset.onset_detect(y=y, sr=sr, units="time", backtrack=True)
    except Exception as e:  # noqa: BLE001
        print(f"[segment] audio onsets unavailable ({e}); using wrist-speed peaks only")
        return np.array([])


def detect_strokes(video_path: Path, data: dict | None = None) -> list[dict]:
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

    onsets = _audio_onset_times(video_path)
    onset_frames = np.round(onsets * fps).astype(int) if onsets.size else np.array([])
    win = int(config.STROKE_WINDOW_SEC * fps)
    snap_tol = int(config.AUDIO_SNAP_TOL_SEC * fps)

    strokes = []
    for i, peak in enumerate(peaks):
        contact = int(peak)
        snapped = False
        if onset_frames.size:
            j = int(np.argmin(np.abs(onset_frames - peak)))
            if abs(int(onset_frames[j]) - peak) <= snap_tol:
                contact = int(onset_frames[j]); snapped = True
        strokes.append({
            "stroke_no": i + 1,
            "peak_frame": int(peak),
            "contact_frame": contact,
            "time_s": round(contact / fps, 2),
            "contact_from_audio": snapped,
            "window": (max(0, contact - win), min(n - 1, contact + win)),
        })

    print(f"[segment] {len(strokes)} strokes detected "
          f"(speed>{height:.2f}, min gap {config.STROKE_MIN_GAP_SEC}s, "
          f"{int((np.array([s['contact_from_audio'] for s in strokes])).sum()) if strokes else 0} audio-refined)")
    return strokes
