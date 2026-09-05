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
# When ballgate anchored the contact on the observed ball direction change, trust it more:
SNAP_SEC_TIGHT = 0.1

REF_NAME = "Alcaraz"
REF_KNEE = 135.0
REF_WING = "forehand"        # the wing REF_KNEE was actually measured on
REF_MATCHED = True           # False => REF_KNEE is the generic threshold below,
                             #          NOT this pro's number for this wing
GENERIC_LOADED_KNEE = 135.0  # coaching guideline used when no wing-matched pro
                             # reference exists; deliberately the same figure the
                             # measured forehand benchmarks cluster at (Alcaraz
                             # 134.8, Eala 133.9) so verdicts stay stable — what
                             # changes is that we stop crediting it to the pro
LOAD_MARGIN = 12             # knee <= ref + margin counts as LOADED

LTH = (0.45, 0.32); DROP = (0.28, 0.18); FIN = (0.03, -0.05)

GREEN, YELLOW, RED = "good", "ok", "bad"


def grade(val, hi, lo, labels):
    if val >= hi:
        return labels[0], GREEN, 1.0
    if val >= lo:
        return labels[1], YELLOW, 0.5
    return labels[2], RED, 0.0


def analyse(c, hf, nn, ww, W, H, fps, snap_sec=None):
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
    near = [f for f in win if abs(f - c) <= max(1, int((snap_sec or SNAP_SEC) * fps))]
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


def median_swing(results):
    """Median-scored swing (lower middle for even counts)."""
    ordered = sorted(results, key=score_of)
    return ordered[(len(ordered) - 1) // 2]


# ---------------------------------------------------------------------------
# Service additions
# ---------------------------------------------------------------------------
def load_reference(name: str) -> dict:
    """Load a pro reference json shipped with the package."""
    path = Path(__file__).resolve().parent / "references" / f"reference_{name}.json"
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def set_reference(name: str, display: str | None = None,
                  stroke: str = "forehand") -> float:
    """Point the leg-load benchmark at a pro reference for THIS wing.

    A forehand and a two-handed backhand are hit from different stances and
    load the legs differently, so a forehand knee mean is not a backhand
    benchmark. Neither shipped reference has been measured on a backhand yet
    (the ball gate verifies 0/145 candidate backhands on the Eala practice
    clip, so there is no honest way to derive one from that footage).

    When the reference carries no entry for this wing we fall back to a
    GENERIC coaching threshold and set REF_MATCHED = False — callers must not
    then attribute the number to the pro.
    """
    global REF_NAME, REF_KNEE, REF_WING, REF_MATCHED
    ref = load_reference(name)
    REF_NAME = display or name.capitalize()
    REF_WING = stroke
    mean = ref.get(f"{stroke}_mean") or {}
    knee = mean.get("knee_flexion")
    if knee is not None:
        REF_KNEE, REF_MATCHED = float(knee), True
    else:
        REF_KNEE, REF_MATCHED = GENERIC_LOADED_KNEE, False
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


def swing_speed_kmh(a: dict, hf: dict, ww: np.ndarray, fps: float) -> float | None:
    """Peak dominant-hand speed (km/h) around contact, from METRIC world landmarks.

    Honest hand speed, not racket-head speed (which runs ~1.5-2x higher) — same
    measurement the desktop highlight reel ships. Adjacent-pair averaging kills
    single-frame pose spikes without clipping the genuine 2-4 frame whip the way
    median-of-3 does at 60fps (validated on the james-hitting cache: median-of-3
    read ~12% low). Values outside 5-60 km/h are pose glitches and return None.
    """
    wi = LM[f"{config.DOMINANT_HAND}_wrist"]
    c = a["contact"]
    lo, hi = c - int(round(0.15 * fps)), c + int(round(0.05 * fps))
    speeds = []
    prev_p, prev_f = None, None
    for f in range(lo, hi + 1):
        if f not in hf:
            continue
        p = ww[hf[f]][wi]
        if np.isnan(p).any():
            prev_p = None
            continue
        if prev_p is not None:
            speeds.append(float(np.linalg.norm(p - prev_p)) * fps / (f - prev_f))
        prev_p, prev_f = p, f
    if len(speeds) < 3:
        return None
    smoothed = [(speeds[i] + speeds[i + 1]) / 2 for i in range(len(speeds) - 1)]
    kmh = max(smoothed) * 3.6
    if not (5.0 <= kmh <= 60.0):
        return None
    return round(kmh, 1)


def contact_front_cm(a: dict, hf: dict, ww: np.ndarray) -> float | None:
    """Signed distance (cm) of the contact point ahead of the hip centre, measured
    along the horizontal swing direction. Positive = met out in front (the classic
    coaching cue); negative = late/behind. World landmarks are hip-origin metric,
    so the wrist vector at contact IS the offset from the body centre."""
    wi = LM[f"{config.DOMINANT_HAND}_wrist"]
    c = a["contact"]
    if c not in hf:
        return None
    p = ww[hf[c]][wi]
    if np.isnan(p).any():
        return None
    f0 = next((f for f in (c - 1, c - 2) if f in hf and not np.isnan(ww[hf[f]][wi]).any()), None)
    f1 = next((f for f in (c + 1, c + 2) if f in hf and not np.isnan(ww[hf[f]][wi]).any()), None)
    if f0 is None or f1 is None:
        return None
    v = ww[hf[f1]][wi] - ww[hf[f0]][wi]
    v[1] = 0.0                                   # horizontal plane only
    n = float(np.linalg.norm(v))
    if n < 1e-6:
        return None
    hips = (ww[hf[c]][LM["left_hip"]] + ww[hf[c]][LM["right_hip"]]) / 2
    d = float(np.dot(p - hips, v / n)) * 100.0
    if not (-40.0 <= d <= 80.0):                 # outside = pose glitch, not biomechanics
        return None
    return round(d, 1)


def front_verdict(front_cm: float | None):
    """('OUT FRONT'|'CLOSE'|'BEHIND', grade) for a contact_front_cm value."""
    if front_cm is None:
        return None
    if front_cm >= 15.0:
        return ("OUT FRONT", GREEN)
    if front_cm >= 0.0:
        return ("CLOSE", YELLOW)
    return ("BEHIND", RED)


CONTACT_FRONT_DRILL = ("Drop-feed and freeze at contact: racket meets the ball a "
                       "racket-length ahead of your front hip, check it 10x.")


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


MAX_GRADED = 30


def grade_all(strokes: list[dict], nn: np.ndarray, ww: np.ndarray,
              W: int, H: int, fps: float, wing: str, max_graded: int = MAX_GRADED):
    """Grade EVERY stroke of the requested wing (honest distribution, no score bias).

    If more than max_graded match, an evenly TIME-spaced subset is graded — never a
    score-ranked one, which would flatter the averages. Returns (graded, stats) where
    graded = [(stroke, analysis)] in chronological order.
    """
    hf = {f: f for f in range(nn.shape[0])}
    counts = {"forehand": 0, "backhand": 0, "ambiguous": 0}
    matching = []
    for s in strokes:
        w = wing_of(s["contact_frame"], nn, fps, W, H)
        counts[w] += 1
        if w == wing:
            matching.append(s)

    picked = matching
    if len(matching) > max_graded:
        idxs = sorted(set(np.linspace(0, len(matching) - 1, max_graded).round().astype(int).tolist()))
        picked = [matching[i] for i in idxs]

    graded, dropped = [], 0
    for s in picked:
        c = s["contact_frame"]
        if not (0 <= c < nn.shape[0]) or np.isnan(nn[c][LM["left_ankle"], 1]):
            dropped += 1
            continue
        try:
            snap = SNAP_SEC_TIGHT if s.get("contact_from_ball") else None
            a = analyse(c, hf, nn, ww, W, H, fps, snap_sec=snap)
        except (ValueError, KeyError, IndexError):
            dropped += 1
            continue
        graded.append((s, a))

    stats = {"counts": counts, "matching": counts.get(wing, 0),
             "considered": len(picked), "graded": len(graded), "dropped": dropped}
    return graded, stats


# BlazePose skeleton for the in-browser replay — same minimal connection set the
# desktop annotate.py uses, re-indexed onto the 14 landmarks we actually ship.
_SKEL_LMS = [11, 12, 13, 14, 15, 16, 23, 24, 25, 26, 27, 28, 31, 32]
_RAW_CONNECTIONS = [
    (11, 12), (11, 13), (13, 15), (12, 14), (14, 16),   # arms + shoulders
    (11, 23), (12, 24), (23, 24),                        # torso
    (23, 25), (25, 27), (24, 26), (26, 28),              # legs
    (27, 31), (28, 32),                                  # feet
]
SKELETON_CONNECTIONS = [[_SKEL_LMS.index(a), _SKEL_LMS.index(b)] for a, b in _RAW_CONNECTIONS]


def skeleton_wrist_index(hand: str) -> int:
    return _SKEL_LMS.index(LM[f"{hand}_wrist"])


def skeleton_frames(a: dict, hf: dict, nn: np.ndarray, step: int = 2) -> dict:
    """Downsampled 14-landmark wireframe over the analysed window, for the browser
    replay. Coordinates are raw image-normalized [x, y] (the client scales to its
    canvas by the bounding box); frames with missing pose are null."""
    frames_used = a["win"][::step]
    out = []
    for f in frames_used:
        row = nn[hf[f]]
        if np.isnan(row[_SKEL_LMS, :2]).any():
            out.append(None)
        else:
            out.append([[round(float(row[i, 0]), 4), round(float(row[i, 1]), 4)]
                        for i in _SKEL_LMS])

    def idx_of(frame):
        return int(min(range(len(frames_used)), key=lambda k: abs(frames_used[k] - frame)))

    return {"frames": out, "contact_i": idx_of(a["contact"]),
            "low_i": idx_of(a["low_f"]), "fin_i": idx_of(a["fin_f"])}


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


def aggregate(results: list[dict], ref_name: str, ref_knee: float,
              speeds: list | None = None, fronts: list | None = None) -> dict:
    """Session-level aggregate for the report card (mirrors coach_reel's report logic).

    speeds/fronts: optional per-swing hand_speed_kmh / contact_front_cm lists aligned
    with results (None entries allowed) — v3 additions, all output fields optional.
    """
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
    med = median_swing(results) if results else None

    def swing_stats(r):
        return {"score": round(score_of(r), 2),
                "knee_deg": None if np.isnan(r["knee"]) else round(float(r["knee"]), 1)}

    # --- v3: consistency + within-session fade (results arrive chronological) ---
    scores = [score_of(r) for r in results]
    sd = float(np.std(scores)) if n >= 3 else None
    if sd is None:
        rating = None
    elif sd < 0.10:
        rating = "machine-like"
    elif sd < 0.18:
        rating = "steady"
    else:
        rating = "streaky"
    consistency = {"score_sd": round(sd, 2) if sd is not None else None, "rating": rating}
    fatigue = None
    if n >= 6:
        third = n // 3
        first_avg = float(np.mean(scores[:third]))
        last_avg = float(np.mean(scores[-third:]))
        fatigue = {"first_third_avg": round(first_avg, 2),
                   "last_third_avg": round(last_avg, 2),
                   "fade": round(first_avg - last_avg, 2)}

    # --- v3: hand speed + contact-front tiles (pose-glitch entries are None) ---
    speed_block = None
    sp = [s for s in (speeds or []) if s is not None]
    if sp:
        best_i = results.index(best) if best is not None else None
        speed_block = {"max_kmh": round(max(sp), 1),
                       "avg_kmh": round(float(np.mean(sp)), 1),
                       "best_swing_kmh": (speeds[best_i] if best_i is not None
                                          and speeds[best_i] is not None else None),
                       "measured": len(sp)}
    front_block = None
    fr = [f for f in (fronts or []) if f is not None]
    if fr:
        front_block = {"avg_cm": round(float(np.mean(fr)), 1),
                       "out_front": sum(1 for f in fr if f >= 15.0),
                       "close": sum(1 for f in fr if 0.0 <= f < 15.0),
                       "behind": sum(1 for f in fr if f < 0.0),
                       "measured": len(fr)}

    # --- v3: session plan — top-2 measured issues, plus contact-front if it's rife ---
    focus = [{"key": k, "sentence": PRIORITY_SENTENCES[k], "drill": DRILL_MAP[k],
              "count": c, "of": n} for c, k in issues[:2]]
    if front_block and front_block["behind"] >= max(2, front_block["measured"] // 3) \
            and len(focus) < 2:
        focus.append({"key": "late contact point",
                      "sentence": "Priority this week: meet the ball further out in front.",
                      "drill": CONTACT_FRONT_DRILL,
                      "count": front_block["behind"], "of": front_block["measured"]})
    plan = {"focus": focus}

    return {
        "n": n,
        "good_hand_path": int(good),
        "distribution": {
            "scores": [round(score_of(r), 2) for r in results],
            "avg_score": round(float(np.mean([score_of(r) for r in results])), 2) if results else None,
            "median_score": round(score_of(med), 2) if med is not None else None,
            "best": swing_stats(best) if best is not None else None,
            "typical": swing_stats(med) if med is not None else None,
            "median_swing_idx": results.index(med) + 1 if med is not None else None,
        },
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
                # False => ref_knee is a generic coaching threshold, not this
                # pro's number for this wing. Do not label it with their name.
                "ref_matched": REF_MATCHED,
                "ref_wing": REF_WING,
                "ref_label": (f"{ref_name} {round(ref_knee)}°" if REF_MATCHED
                              else f"loaded under {round(ref_knee + LOAD_MARGIN)}°"),
            },
        },
        "verdict": verdict,
        "issues": [{"key": k, "count": c} for c, k in issues],
        "priority": priority,
        "best_swing_idx": results.index(best) + 1 if best is not None else None,
        "consistency": consistency,
        "fatigue": fatigue,
        "speed": speed_block,
        "contact_front": front_block,
        "plan": plan,
    }
