"""Pose extraction — vendored from tennis-coach src/extract_pose.py.

Changes for the service: package-relative imports, optional progress callback in
extract(), model path resolution via config.pose_model_path().

Cache layout (compact NumPy arrays):
  norm  : float32 [N, 33, 4]  normalized image landmarks x,y,z,visibility
  world : float32 [N, 33, 3]  metric 3D landmarks, hip-centred (for angles)
Frames where no pose was found are stored as all-NaN rows.
"""
from pathlib import Path

import cv2
import numpy as np
import mediapipe as mp
from mediapipe.tasks import python as mp_python
from mediapipe.tasks.python import vision

from . import config


def frame_has_pose(world_row) -> bool:
    return not bool(np.isnan(world_row).any())


def _build_landmarker(model_path=None):
    model_path = model_path or config.POSE_MODEL_PATH
    if not model_path.exists():
        raise FileNotFoundError(f"Pose model not found at {model_path}.")
    base = mp_python.BaseOptions(model_asset_path=str(model_path))
    # IMAGE mode: each frame detected independently so the moving crop can change
    # freely frame-to-frame (VIDEO mode's internal ROI tracking fights our crop).
    options = vision.PoseLandmarkerOptions(
        base_options=base,
        running_mode=vision.RunningMode.IMAGE,
        num_poses=1,
        min_pose_detection_confidence=0.5,
    )
    return vision.PoseLandmarker.create_from_options(options)


def _clamp_box(x, y, w, h, W, H):
    w = min(int(round(w)), W)
    h = min(int(round(h)), H)
    x = int(round(min(max(0, x), W - w)))
    y = int(round(min(max(0, y), H - h)))
    return x, y, w, h


def _player_box_from_norm(norm_full, W, H):
    vis = norm_full[:, 3] > 0.3
    pts = norm_full[vis] if vis.sum() >= 8 else norm_full
    xs = pts[:, 0] * W
    ys = pts[:, 1] * H
    return xs.min(), ys.min(), xs.max(), ys.max()


def _next_crop(bx, by, bx2, by2, W, H):
    cw = max((bx2 - bx) * config.CROP_PAD, config.CROP_MIN_W)
    ch = max((by2 - by) * config.CROP_PAD, config.CROP_MIN_H)
    cx = (bx + bx2) / 2
    cy = (by + by2) / 2
    return _clamp_box(cx - cw / 2, cy - ch / 2, cw, ch, W, H)


def extract(video_path: Path, progress_cb=None) -> dict:
    """Process a video, return a dict and write it to DATA_DIR/<clip>_landmarks.npz.

    progress_cb: optional callable(idx, total) invoked every 300 frames.
    """
    video_path = Path(video_path)
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0

    landmarker = _build_landmarker()
    norm_list, world_list = [], []
    nan_norm = np.full((33, 4), np.nan, np.float32)
    nan_world = np.full((33, 3), np.nan, np.float32)
    idx = 0
    detected = 0
    seed = config.crop_for(video_path.stem)
    box = tuple(seed) if (config.AUTO_CROP and seed) else None
    print(f"[extract_pose] {video_path.name}: {fps:.2f} fps, {width}x{height}, ~{total} frames"
          f"  (auto_crop={config.AUTO_CROP})")

    while True:
        ok, frame_bgr = cap.read()
        if not ok:
            break

        if config.AUTO_CROP and box is not None:
            x0, y0, cw, ch = box
        else:
            x0, y0, cw, ch = 0, 0, width, height
        crop_bgr = frame_bgr[y0:y0 + ch, x0:x0 + cw]
        crop_rgb = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2RGB)
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=crop_rgb)
        result = landmarker.detect(mp_image)

        if result.pose_landmarks:
            nf = np.array([[(x0 + lm.x * cw) / width, (y0 + lm.y * ch) / height,
                            lm.z, lm.visibility] for lm in result.pose_landmarks[0]],
                          np.float32)
            norm_list.append(nf)
            world_list.append(np.array(
                [[lm.x, lm.y, lm.z] for lm in result.pose_world_landmarks[0]], np.float32)
                if result.pose_world_landmarks else nan_world.copy())
            detected += 1
            if config.AUTO_CROP:
                bx, by, bx2, by2 = _player_box_from_norm(nf, width, height)
                box = _next_crop(bx, by, bx2, by2, width, height)
        else:
            norm_list.append(nan_norm.copy())
            world_list.append(nan_world.copy())
            if config.AUTO_CROP:
                if seed:
                    box = tuple(seed)
                elif box is not None:
                    x, y, w, h = box
                    g = config.CROP_REACQUIRE_GROW
                    nb = _clamp_box(x - w * (g - 1) / 2, y - h * (g - 1) / 2,
                                    w * g, h * g, width, height)
                    box = None if (nb[2] >= width and nb[3] >= height) else nb

        idx += 1
        if idx % 300 == 0:
            pct = f" ({100*idx/total:.0f}%)" if total else ""
            print(f"  ...{idx} frames{pct}, pose in {detected}")
            if progress_cb:
                progress_cb(idx, total)

    cap.release()
    landmarker.close()

    norm = np.stack(norm_list) if norm_list else np.zeros((0, 33, 4), np.float32)
    world = np.stack(world_list) if world_list else np.zeros((0, 33, 3), np.float32)
    detected = int(np.sum(~np.isnan(world[:, 0, 0]))) if idx else 0
    print(f"[extract_pose] done: {idx} frames, pose found in {detected}")

    out_path = config.DATA_DIR / f"{video_path.stem}_landmarks.npz"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out_path, norm=norm, world=world,
                        fps=fps, width=width, height=height, n_frames=idx,
                        video=str(video_path))
    return {"video": str(video_path), "fps": fps, "width": width, "height": height,
            "n_frames": idx, "norm": norm, "world": world}


def extract_windows(video_path: Path, windows, seed_norm=None, variant="heavy",
                    crop_pad=1.3, min_w=360, min_h=520, cache_tag="fh_heavy"):
    """Re-extract pose over specific [lo, hi] frame windows at higher fidelity.
    Returns and caches arrays: frames [M] (sorted unique), norm [M,33,4], world [M,33,3].
    seed_norm: full-session norm array used to seed each window's first crop."""
    video_path = Path(video_path)
    cache = config.DATA_DIR / f"{video_path.stem}_{cache_tag}.npz"
    want_all = {f for lo, hi in windows for f in range(lo, hi + 1) if f >= 0}
    if cache.exists():
        z = np.load(cache, allow_pickle=True)
        if want_all.issubset(set(int(f) for f in z["frames"])):
            return {"frames": z["frames"], "norm": z["norm"], "world": z["world"]}

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video: {video_path}")
    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    landmarker = _build_landmarker(config.pose_model_path(variant))

    def tight_crop(bx, by, bx2, by2):
        cw = max((bx2 - bx) * crop_pad, min_w)
        ch = max((by2 - by) * crop_pad, min_h)
        return _clamp_box((bx + bx2) / 2 - cw / 2, (by + by2) / 2 - ch / 2, cw, ch, W, H)

    def seed_box(frame_idx):
        if seed_norm is None:
            return None
        for cand in range(frame_idx, min(frame_idx + 30, seed_norm.shape[0])):
            row = seed_norm[cand]
            if not np.isnan(row).any():
                bx, by, bx2, by2 = _player_box_from_norm(row, W, H)
                return tight_crop(bx, by, bx2, by2)
        return None

    want = sorted({f for lo, hi in windows for f in range(lo, hi + 1) if 0 <= f})
    frames_out, norm_out, world_out = [], [], []
    nan_n = np.full((33, 4), np.nan, np.float32)
    nan_w = np.full((33, 3), np.nan, np.float32)

    box, detected = None, 0
    for lo, hi in sorted(windows):
        box = seed_box(lo)
        cap.set(cv2.CAP_PROP_POS_FRAMES, lo)
        for idx in range(lo, hi + 1):
            ok, frame = cap.read()
            if not ok:
                break
            x0, y0, cw, ch = box if box else (0, 0, W, H)
            crop = cv2.cvtColor(frame[y0:y0 + ch, x0:x0 + cw], cv2.COLOR_BGR2RGB)
            res = landmarker.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=crop))
            if res.pose_landmarks:
                nf = np.array([[(x0 + lm.x * cw) / W, (y0 + lm.y * ch) / H, lm.z, lm.visibility]
                               for lm in res.pose_landmarks[0]], np.float32)
                wf = (np.array([[lm.x, lm.y, lm.z] for lm in res.pose_world_landmarks[0]],
                               np.float32) if res.pose_world_landmarks else nan_w.copy())
                detected += 1
                bx, by, bx2, by2 = _player_box_from_norm(nf, W, H)
                box = tight_crop(bx, by, bx2, by2)
            else:
                nf, wf = nan_n.copy(), nan_w.copy()
                box = seed_box(idx)
            frames_out.append(idx); norm_out.append(nf); world_out.append(wf)
    cap.release(); landmarker.close()

    frames = np.array(frames_out, np.int32)
    norm = np.stack(norm_out) if norm_out else np.zeros((0, 33, 4), np.float32)
    world = np.stack(world_out) if world_out else np.zeros((0, 33, 3), np.float32)
    cache.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(cache, frames=frames, norm=norm, world=world)
    print(f"[extract_windows] {len(frames)} frames over {len(windows)} windows "
          f"({variant}, pad {crop_pad}); pose in {detected}")
    return {"frames": frames, "norm": norm, "world": world}


def load(video_path: Path, progress_cb=None) -> dict:
    """Load cached landmarks for a clip, or extract if missing."""
    video_path = Path(video_path)
    cache = config.DATA_DIR / f"{video_path.stem}_landmarks.npz"
    if cache.exists():
        z = np.load(cache, allow_pickle=True)
        return {"video": str(z["video"]), "fps": float(z["fps"]),
                "width": int(z["width"]), "height": int(z["height"]),
                "n_frames": int(z["n_frames"]), "norm": z["norm"], "world": z["world"]}
    return extract(video_path, progress_cb=progress_cb)
