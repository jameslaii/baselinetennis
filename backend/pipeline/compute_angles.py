"""Biomechanical angle / metric computation — vendored from tennis-coach.

Works on the metric 3D 'world' landmarks (hip-centred, in metres). Only the
package-relative import differs from the desktop original; flag()/IDEAL_RANGES
(report colouring) are not used by the service and were dropped.
"""
import numpy as np

from . import config

LM = config.LM


def angle(a, b, c) -> float:
    """Interior angle ABC in degrees, at vertex b, from three (x,y,z) points."""
    a, b, c = map(np.asarray, (a, b, c))
    ba, bc = a - b, c - b
    cos = np.dot(ba, bc) / (np.linalg.norm(ba) * np.linalg.norm(bc) + 1e-9)
    return float(np.degrees(np.arccos(np.clip(cos, -1.0, 1.0))))


def _line_angle_to_vertical(p_top, p_bottom) -> float:
    v = np.asarray(p_top) - np.asarray(p_bottom)
    vertical = np.array([0.0, -1.0, 0.0])  # MediaPipe y grows downward
    cos = np.dot(v, vertical) / (np.linalg.norm(v) * np.linalg.norm(vertical) + 1e-9)
    return float(np.degrees(np.arccos(np.clip(cos, -1.0, 1.0))))


def _x_factor(world) -> float:
    ls = np.array(world[LM["left_shoulder"]]); rs = np.array(world[LM["right_shoulder"]])
    lh = np.array(world[LM["left_hip"]]);      rh = np.array(world[LM["right_hip"]])
    shoulder_vec = (ls - rs)[[0, 2]]
    hip_vec = (lh - rh)[[0, 2]]
    cos = np.dot(shoulder_vec, hip_vec) / (
        np.linalg.norm(shoulder_vec) * np.linalg.norm(hip_vec) + 1e-9)
    return float(np.degrees(np.arccos(np.clip(cos, -1.0, 1.0))))


def metrics_for_frame(world) -> dict:
    """Compute all coaching metrics from one frame's world landmarks."""
    h = config.DOMINANT_HAND
    elbow = angle(world[LM[f"{h}_shoulder"]], world[LM[f"{h}_elbow"]], world[LM[f"{h}_wrist"]])
    knee = angle(world[LM[f"{h}_hip"]], world[LM[f"{h}_knee"]], world[LM[f"{h}_ankle"]])

    sh_mid = (np.array(world[LM["left_shoulder"]]) + np.array(world[LM["right_shoulder"]])) / 2
    hip_mid = (np.array(world[LM["left_hip"]]) + np.array(world[LM["right_hip"]])) / 2
    trunk_lean = _line_angle_to_vertical(sh_mid, hip_mid)

    wrist = np.array(world[LM[f"{h}_wrist"]])
    contact_forward = float(hip_mid[2] - wrist[2])

    return {
        "elbow_angle": round(elbow, 1),
        "knee_flexion": round(knee, 1),
        "trunk_lean_deg": round(trunk_lean, 1),
        "x_factor_deg": round(_x_factor(world), 1),
        "contact_forward_m": round(contact_forward, 3),
    }


def metrics_at_contact(data: dict, contact_frame: int) -> dict:
    """Pick the contact frame (or nearest frame that has pose) and compute metrics."""
    world = data["world"]
    n = world.shape[0]
    for offset in range(0, max(n, 1)):
        for cand in (contact_frame + offset, contact_frame - offset):
            if 0 <= cand < n and not np.isnan(world[cand]).any():
                m = metrics_for_frame(world[cand])
                m["_used_frame"] = cand
                return m
    return {}
