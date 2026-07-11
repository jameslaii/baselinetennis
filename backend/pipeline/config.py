"""Central configuration — vendored from tennis-coach for the Baseline web service.

Differences from the desktop original:
  * MODELS_DIR / DATA_DIR are env-overridable (BASELINE_MODELS_DIR / BASELINE_DATA_DIR);
    the worker also mutates DATA_DIR per job before extraction.
  * Per-clip override dicts are EMPTY — the service handles arbitrary uploads, so only
    the safe defaults apply (full-frame adaptive crop, handedness set from the upload form).
"""
import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = Path(os.environ.get("BASELINE_MODELS_DIR", PROJECT_ROOT / "models"))
DATA_DIR = Path(os.environ.get("BASELINE_DATA_DIR", PROJECT_ROOT / "data"))

# ---------------------------------------------------------------------------
# Pose model variant. "heavy" = most accurate (short windows), "full" = ~2-3x faster
# on CPU (long full-clip scans), "lite" = fastest.
# ---------------------------------------------------------------------------
MODEL_VARIANT = "full"  # "lite" | "full" | "heavy"

MODEL_URL_TMPL = (
    "https://storage.googleapis.com/mediapipe-models/pose_landmarker/"
    "pose_landmarker_{v}/float16/latest/pose_landmarker_{v}.task"
)


def pose_model_path(variant: str | None = None) -> Path:
    return MODELS_DIR / f"pose_landmarker_{variant or MODEL_VARIANT}.task"


POSE_MODEL_PATH = pose_model_path()

# ---------------------------------------------------------------------------
# Handedness: which hand holds the racket. The worker sets DOMINANT_HAND from the
# upload form before any analysis (there is no reliable auto-detection).
# ---------------------------------------------------------------------------
DOMINANT_HAND = "right"  # "right" or "left"
DOMINANT_HAND_PER_CLIP: dict[str, str] = {}


def hand_for(stem: str) -> str:
    """Racket hand for a clip: per-clip override if present, else the default."""
    return DOMINANT_HAND_PER_CLIP.get(stem, DOMINANT_HAND)

# ---------------------------------------------------------------------------
# Auto-crop — follows the player with a moving crop so they stay large in the ROI.
# No per-clip seeds in the service: starts full-frame, locks on after first detection,
# widens the search box on loss.
# ---------------------------------------------------------------------------
AUTO_CROP = True
CROP = None
CROPS: dict[str, tuple | None] = {}
CROP_PAD = 1.7
CROP_MIN_W = 480
CROP_MIN_H = 680
CROP_REACQUIRE_GROW = 1.7


def crop_for(stem: str):
    """Seed/fallback crop for a clip: per-clip CROPS entry if present, else CROP."""
    return CROPS[stem] if stem in CROPS else CROP

# ---------------------------------------------------------------------------
# MediaPipe Pose landmark indices (BlazePose 33-point model).
# ---------------------------------------------------------------------------
LM = {
    "nose": 0,
    "left_shoulder": 11, "right_shoulder": 12,
    "left_elbow": 13, "right_elbow": 14,
    "left_wrist": 15, "right_wrist": 16,
    "left_hip": 23, "right_hip": 24,
    "left_knee": 25, "right_knee": 26,
    "left_ankle": 27, "right_ankle": 28,
    "left_foot_index": 31, "right_foot_index": 32,
}

# ---------------------------------------------------------------------------
# Long-footage segmentation (same validated values as the desktop pipeline).
# ---------------------------------------------------------------------------
STROKE_MIN_GAP_SEC = 1.3
STROKE_SPEED_PERCENTILE = 80
STROKE_WINDOW_SEC = 1.0

# FH/BH midline classification defaults (kept for compute_angles compatibility;
# the service classifies wings by inter-wrist gap instead — see grading.wing_of).
FOREHAND_ON_WRIST_POSITIVE_X = True
FH_POSITIVE_X: dict[str, bool] = {}


def fh_positive_for(stem: str) -> bool:
    return FH_POSITIVE_X[stem] if stem in FH_POSITIVE_X else FOREHAND_ON_WRIST_POSITIVE_X
