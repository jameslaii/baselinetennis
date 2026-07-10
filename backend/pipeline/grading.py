"""Swing grading — the validated metric logic from tennis-coach, headless.

analyse() and its tunables are copied VERBATIM from handpath_reel.py (the "one source
of metric truth" that coach_reel also imports), with one mechanical change: the BGR
colour tuples GREEN/YELLOW/RED become the strings "good"/"ok"/"bad" (analyse only
carries them as tags; nothing draws here). DRILL_MAP / PRIORITY_SENTENCES /
_rank_issues / score_of / best_swing come from coach_reel.py.

New service code: wing_of() (inter-wrist-gap wing filter), rank_candidates()
(automatic top-N selection replacing the desktop's hand-verified contact lists),
hand_path_points() and aggregate() (results-JSON assembly).
"""
import json
from pathlib import Path

import numpy as np

from . import config
from . import compute_angles

LM = config.LM

# ---------------------------------------------------------------------------
# analyse() tunables — verbatim from handpath_reel.py
# ---------------------------------------------------------------------------
WIN_SEC = 0.7                # half-window analyzed around each contact
PRE_SEC = 0.5                # how far before contact to search for the hand LOW point
POST_SEC = 0.4               # how far after contact to search for the FINISH
SNAP_SEC = 0.3               # re-pick contact at the fastest wrist frame within this radius
# When contacts are audio-refined (segment snaps to the ball "pock"), trust them more:
SNAP_SEC_AUDIO = 0.1

REF_NAME = "Alcaraz"
REF_KNEE = 135.0
LOAD_MARGIN = 12             # knee <= ref + margin counts as LOADED

LTH = (0.45, 0.32); DROP = (0.28, 0.18); FIN = (0.03, -0.05)

GREEN, YELLOW, RED = "good", "ok", "bad"


def grade(val, hi, lo, labels):
    if val >= hi:
        return labels[0], GREEN, 1.0
    if val >= lo:
        return labels[1], YELLOW, 0.5
    return labels[2], RED, 0.0


def analyse(c, hf, nn, ww, W, H, fps):
    wi = LM[f"{config.DOMINANT_HAND}_wrist"]
    win = [f for f in range(c - int(WIN_SEC * fps), c + int(WIN_SEC * fps)) if f in hf]
    # glitch cap scales with resolution: genuine contact-speed is ~60-110 px/frame at 4K/60,
    # so a fixed 50px (tuned on 1080p/120) zeroes real motion and mis-picks the contact frame.
    cap = max(50, W * 0.03)
    spd = {}
    for k in range(1, len(win)):
        a, b = nn[hf[win[k]]][wi, :2], nn[hf[win[k-1]]][wi, :2]
        if not np.isnan(a).any() and not np.isnan(b).any():
            d = np.hypot((a[0]-b[0])*W, (a[1]-b[1])*H); spd[win[k]] = d if d < cap else 0
    near = [f for f in win if abs(f - c) <= max(1, int(SNAP_SEC * fps))]
    cc = max(near, key=lambda f: spd.get(f, 0)) if near else c
    r = nn[hf[cc]]
    body = abs((r[LM['left_ankle']][1]+r[LM['right_ankle']][1])/2
               - (r[LM['left_shoulder']][1]+r[LM['right_shoulder']][1])/2) * H
    cy = r[wi, 1] * H
    sh_y = (r[LM['left_shoulder']][1]+r[LM['right_shoulder']][1])/2
    hip_y = (r[LM['left_hip']][1]+r[LM['right_hip']][1])/2
    pre = [(f, nn[hf[f]][wi, 1]*H) for f in win if cc-int(PRE_SEC*fps) <= f <= cc and not np.isnan(nn[hf[f]][wi, 1])]
    post = [(f, nn[hf[f]][wi, 1]*H) for f in win if cc <= f <= cc+int(POST_SEC*fps) and not np.isnan(nn[hf[f]][wi, 1])]
    low_f, low_y = max(pre, key=lambda t: t[1])
    fin_f, fin_y = min(post, key=lambda t: t[1])
    drop = (low_y - cy) / body; tot = (low_y - fin_y) / body; fvs = (sh_y*H - fin_y) / body

    lth = grade(tot, *LTH, ("GOOD", "OK", "FLAT"))
    drp = grade(drop, *DROP, ("GOOD", "OK", "SHALLOW"))
    fin = grade(fvs, *FIN, ("HIGH", "OK", "LOW"))
    # On an ankle-height contact you physically can't drop far below the ball —
    # don't count a shallow drop against the swing there.
    chgt_early = (hip_y - r[wi, 1]) / (hip_y - sh_y + 1e-6)
    if drp[2] == 0.0 and chgt_early < 0.30:
        drp = ("LOW BALL", YELLOW, 0.5)
    score = (lth[2] + drp[2] + fin[2]) / 3
    if score >= 0.83:
        overall, ocol, note = "GOOD HAND PATH", GREEN, "Textbook low-to-high - keep it."
    elif score >= 0.5:
        overall, ocol = "DECENT", YELLOW
        note = "Solid - finish higher for more topspin." if fin[2] < 1 else "Solid arc."
    else:
        overall, ocol = "NEEDS WORK", RED
        note = "Drop the racket below the ball more." if drp[2] == 0 else "Brush up, finish over the shoulder."

    # contact height: 0=hip, 1=shoulder, >1 above shoulder
    chgt = (hip_y - r[wi, 1]) / (hip_y - sh_y + 1e-6)
    if chgt < 0.30:
        cverd = ("LOW", RED); good_c = False
    elif chgt > 1.0:
        cverd = ("HIGH/LATE", YELLOW); good_c = False
    else:
        cverd = ("GOOD", GREEN); good_c = True
    # leg load
    knee = compute_angles.metrics_for_frame(ww[hf[cc]])["knee_flexion"] if not np.isnan(ww[hf[cc]]).any() else float('nan')
    loaded = (not np.isnan(knee)) and knee <= REF_KNEE + LOAD_MARGIN
    lverd = ("LOADED", GREEN) if loaded else ("STANDS TALL", YELLOW)

    tag = {GREEN: "GOOD", YELLOW: "OK", RED: "BAD"}[ocol]
    return {"win": win, "contact": cc, "low_f": low_f, "fin_f": fin_f,
            "lth": lth, "drp": drp, "fin": fin, "overall": (overall, ocol), "note": note,
            "tag": tag, "tagcol": ocol,
            "good": score >= 0.83, "flat_finish": fin[2] == 0,
            "chgt": chgt, "cverd": cverd, "good_contact": good_c,
            "knee": knee, "lverd": lverd, "loaded": loaded}


# ---------------------------------------------------------------------------
# Coaching text + ranking helpers — verbatim from coach_reel.py
# ---------------------------------------------------------------------------
DRILL_MAP = {
    "flat finish": "Shadow swings: finish hand above opposite shoulder, hold 2s x 10 before each basket.",
    "shallow drop": "Exaggerate the C-loop: touch racket head to knee height before driving up, 10 slow reps.",
    "stands tall": "Split-step then sit: knees inside ~140 deg before every feed; hold the sit through contact.",
    "high/late contact": "Prep earlier: racket back before the bounce; meet the ball out front, waist-to-shoulder.",
    "low contact": "Adjust feet first: two quick steps so the ball is met in the strike zone, not scooped.",
}

PRIORITY_SENTENCES = {
    "flat finish": "Priority this week: finish every swing above the opposite shoulder.",
    "shallow drop": "Priority this week: deepen the C-loop - drop the racket below the ball first.",
    "stands tall": "Priority this week: sit lower - load the knees before every contact.",
    "high/late contact": "Priority this week: earlier prep - meet the ball out front in the strike zone.",
    "low contact": "Priority this week: move the feet first so contact happens in the strike zone.",
}

CRITERIA_LEGEND = [
    ("RACKET DROP", "hand dips below the ball first"),
    ("LOW-TO-HIGH", "total upward wrist travel"),
    ("FINISH", "hand finishes above the shoulder"),
    ("CONTACT HEIGHT", "strike zone, waist to shoulder"),
    ("LEG LOAD", "knee bend vs the pro at contact"),
]


def _rank_issues(results):
    """Rank this player's actual weaknesses by count; keys index DRILL_MAP/PRIORITY_SENTENCES."""
    n = len(results)
    flat = sum(r["flat_finish"] for r in results)
    shallow = sum(1 for r in results if r["drp"][0] == "SHALLOW")
    stands_tall = n - sum(r["loaded"] for r in results)
    highs = sum(1 for r in results if r["cverd"][0] == "HIGH/LATE")
    lows = sum(1 for r in results if r["cverd"][0] == "LOW")
    issues = []
    if flat:
        issues.append((flat, "flat finish"))
    if shallow:
        issues.append((shallow, "shallow drop"))
    if stands_tall:
        issues.append((stands_tall, "stands tall"))
    if highs:
        issues.append((highs, "high/late contact"))
    if lows:
        issues.append((lows, "low contact"))
    issues.sort(key=lambda t: -t[0])
    return issues


def score_of(a):
    """analyse() hand-path score (same formula handpath_reel uses internally)."""
    return (a["lth"][2] + a["drp"][2] + a["fin"][2]) / 3


def best_swing(results, ref_knee):
    """Best by analyse score; tie-break knee closest to the pro reference."""
    return max(results, key=lambda a: (score_of(a), -abs((a["knee"] if not np.isnan(a["knee"]) else 1e9) - ref_knee)))


# ---------------------------------------------------------------------------
# Service additions
# ---------------------------------------------------------------------------
def load_reference(name: str) -> dict:
    """Load a pro reference json shipped with the package."""
    path = Path(__file__).resolve().parent / "references" / f"reference_{name}.json"
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def set_reference(name: str, display: str | None = None) -> float:
    """Point the leg-load benchmark at a pro reference. Returns the knee value."""
    global REF_NAME, REF_KNEE
    ref = load_reference(name)
    REF_NAME = display or name.capitalize()
    REF_KNEE = float(ref["forehand_mean"]["knee_flexion"])
    return REF_KNEE


def wing_of(contact: int, norm: np.ndarray, fps: float, W: int, H: int) -> str:
    """'forehand' | 'backhand' | 'ambiguous' via inter-wrist gap / shoulder width.

    Validated on real 2HBH footage (window -18/+2 frames @60fps, expressed in seconds):
    both hands on the grip through contact means the wrists stay closer than ~0.95
    shoulder-widths; a forehand swings one wrist far from the other (>~1.2).
    v1 limitation: a ONE-handed backhand reads as 'forehand' here — the product
    advertises forehand + two-handed backhand only.
    """
    lo = max(0, contact - int(round(0.30 * fps)))
    hi = min(norm.shape[0] - 1, contact + int(round(0.033 * fps)))
    ratios = []
    for f in range(lo, hi + 1):
        r = norm[f]
        if np.isnan(r[[LM["left_wrist"], LM["right_wrist"],
                       LM["left_shoulder"], LM["right_shoulder"]], :2]).any():
            continue
        def px(i):
            return np.array([r[i, 0] * W, r[i, 1] * H])
        wrist_gap = np.linalg.norm(px(LM["left_wrist"]) - px(LM["right_wrist"]))
        sh_w = np.linalg.norm(px(LM["left_shoulder"]) - px(LM["right_shoulder"]))
        if sh_w > 1e-6:
            ratios.append(wrist_gap / sh_w)
    if len(ratios) < 5:
        return "ambiguous"
    m = float(np.mean(ratios))
    if m < 0.95:
        return "backhand"
    if m > 1.2:
        return "forehand"
    return "ambiguous"


def peak_wrist_speed(c: int, hf: dict, nn: np.ndarray, W: int, H: int, fps: float) -> float:
    """Glitch-filtered peak wrist speed (px/frame) around a contact — ranking signal only."""
    wi = LM[f"{config.DOMINANT_HAND}_wrist"]
    win = [f for f in range(c - int(WIN_SEC * fps), c + int(WIN_SEC * fps)) if f in hf]
    cap = max(50, W * 0.03)
    best = 0.0
    for k in range(1, len(win)):
        a, b = nn[hf[win[k]]][wi, :2], nn[hf[win[k-1]]][wi, :2]
        if not np.isnan(a).any() and not np.isnan(b).any():
            d = float(np.hypot((a[0]-b[0])*W, (a[1]-b[1])*H))
            if d < cap:
                best = max(best, d)
    return best


def rank_candidates(strokes: list[dict], nn: np.ndarray, ww: np.ndarray,
                    W: int, H: int, fps: float, wing: str,
                    top_n: int = 10, max_candidates: int = 40):
    """Automatic replacement for the desktop's hand-verified contact lists.

    Filters segment-detected strokes to the requested wing, grades each with
    analyse() on the session-level pose, and returns (selected, stats) where
    selected = [(stroke, analysis)] of the top_n by (hand-path score, peak speed).
    """
    hf = {f: f for f in range(nn.shape[0])}
    counts = {"forehand": 0, "backhand": 0, "ambiguous": 0}
    matching = []
    for s in strokes:
        w = wing_of(s["contact_frame"], nn, fps, W, H)
        counts[w] += 1
        if w == wing:
            matching.append(s)

    # keep grading affordable on stroke-dense clips: pre-cut by raw peak speed
    if len(matching) > max_candidates:
        matching = sorted(
            matching,
            key=lambda s: -peak_wrist_speed(s["contact_frame"], hf, nn, W, H, fps),
        )[:max_candidates]

    graded, dropped = [], 0
    for s in matching:
        c = s["contact_frame"]
        if not (0 <= c < nn.shape[0]) or np.isnan(nn[c][LM["left_ankle"], 1]):
            dropped += 1
            continue
        try:
            a = analyse(c, hf, nn, ww, W, H, fps)
        except (ValueError, KeyError, IndexError):
            dropped += 1
            continue
        graded.append((s, a))

    graded.sort(key=lambda t: (-score_of(t[1]),
                               -peak_wrist_speed(t[0]["contact_frame"], hf, nn, W, H, fps)))
    selected = sorted(graded[:top_n], key=lambda t: t[0]["contact_frame"])
    stats = {"counts": counts, "matching": counts.get(wing, 0),
             "graded": len(graded), "dropped": dropped}
    return selected, stats


def hand_path_points(a: dict, hf: dict, nn: np.ndarray, W: int, H: int,
                     max_points: int = 40) -> dict:
    """Wrist trajectory over the analysed window, in body-height units relative to the
    contact point (x right, y UP — flipped from image coords for natural plotting)."""
    wi = LM[f"{config.DOMINANT_HAND}_wrist"]
    cc = a["contact"]
    r = nn[hf[cc]]
    body = abs((r[LM['left_ankle']][1] + r[LM['right_ankle']][1]) / 2
               - (r[LM['left_shoulder']][1] + r[LM['right_shoulder']][1]) / 2) * H
    if not np.isfinite(body) or body < 1e-6:
        return {"points": [], "low": None, "contact": [0.0, 0.0], "finish": None}
    cx, cy = r[wi, 0] * W, r[wi, 1] * H

    def rel(f):
        p = nn[hf[f]][wi, :2]
        if np.isnan(p).any():
            return None
        return [round(float(p[0] * W - cx) / float(body), 3),
                round(-float(p[1] * H - cy) / float(body), 3)]

    frames = [f for f in a["win"] if rel(f) is not None]
    step = max(1, len(frames) // max_points)
    pts = [rel(f) for f in frames[::step]]
    return {
        "points": pts,
        "low": rel(a["low_f"]),
        "contact": [0.0, 0.0],
        "finish": rel(a["fin_f"]),
    }


def _tile_counts(results, key):
    good = sum(1 for r in results if r[key][2] == 1.0)
    ok = sum(1 for r in results if r[key][2] == 0.5)
    bad = sum(1 for r in results if r[key][2] == 0.0)
    return {"good": good, "ok": ok, "bad": bad}


def aggregate(results: list[dict], ref_name: str, ref_knee: float) -> dict:
    """Session-level aggregate for the report card (mirrors coach_reel's report logic)."""
    n = len(results)
    good = sum(r["good"] for r in results)
    knees = [r["knee"] for r in results if not np.isnan(r["knee"])]
    avg_knee = float(np.mean(knees)) if knees else None
    issues = _rank_issues(results)

    frac = good / n if n else 0
    if frac >= 0.6:
        verdict = {"label": "Tour-Shaped Swing", "grade": "good"}
    elif frac >= 0.3:
        verdict = {"label": "Solid Foundations", "grade": "ok"}
    else:
        verdict = {"label": "Work In Progress", "grade": "bad"}

    if issues:
        key = issues[0][1]
        priority = {"key": key, "sentence": PRIORITY_SENTENCES[key], "drill": DRILL_MAP[key]}
    else:
        priority = {"key": None,
                    "sentence": "No recurring weakness found - keep grooving this shape.",
                    "drill": "Rally sets of 20 focusing on repeating the same finish height."}

    best = best_swing(results, ref_knee) if results else None
    return {
        "n": n,
        "good_hand_path": int(good),
        "tiles": {
            "racket_drop": _tile_counts(results, "drp"),
            "low_to_high": _tile_counts(results, "lth"),
            "finish": _tile_counts(results, "fin"),
            "contact_height": {
                "in_zone": sum(1 for r in results if r["good_contact"]),
                "low": sum(1 for r in results if r["cverd"][0] == "LOW"),
                "high_late": sum(1 for r in results if r["cverd"][0] == "HIGH/LATE"),
            },
            "leg_load": {
                "loaded": sum(1 for r in results if r["loaded"]),
                "avg_knee_deg": round(avg_knee, 1) if avg_knee is not None else None,
                "ref_knee_deg": round(ref_knee, 1),
                "delta_deg": round(avg_knee - ref_knee, 1) if avg_knee is not None else None,
            },
        },
        "verdict": verdict,
        "issues": [{"key": k, "count": c} for c, k in issues],
        "priority": priority,
        "best_swing_idx": results.index(best) + 1 if best is not None else None,
    }
