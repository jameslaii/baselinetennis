"""Vision ball-hit verification — pure-CV replacement for the audio "pock" path.

For every candidate stroke (a dominant-wrist speed peak from segment.detect_strokes)
this module re-reads a short window of frames and looks for a small fast-moving blob
whose trajectory approaches the player's racket reach and changes direction there —
i.e. an actual ball being struck. Swings with no such trajectory are practice swings,
pickups or misfires and get contact_from_ball=False.

The gate is ADAPTIVE at session level: if the detector confirms hits on a reasonable
share of candidates the clip is considered ball-trackable and unverified swings can be
excluded; if it confirms almost none, the ball is probably not visible (far camera,
motion blur, ball out of frame) and the caller should keep all swings but warn.

No ML, no new dependencies: cv2 + numpy only, all thresholds scale with player size
and frame rate so 30/60/120 fps and 720p-4K uploads behave alike.
"""
from pathlib import Path

import cv2
import numpy as np

from . import config

# Window read around each wrist-speed peak (contact sits at/near the peak).
PRE_S, POST_S = 0.50, 0.35
PROC_W = 960                 # analysis width; everything below is tuned at this scale
DIFF_THRESH = 25             # three-frame difference binarization
MIN_TRACK_PTS = 4
MIN_SPEED_PXS = 400          # scaled px/sec — even lazy feeds exceed this by far
TURN_DEG = 45                # direction change at the racket that counts as a hit
REACH_ARM_MULT = 1.6         # racket reach = 1.6x shoulder->wrist distance
MIN_CONFIDENCE = 0.35
GATE_MIN_STROKES = 4         # below this, gate stays active if >=1 hit
GATE_MIN_HIT_FRAC = 0.25     # under this fraction the detector is considered blind

_CORE_LMS = [config.LM[k] for k in (
    "nose", "left_shoulder", "right_shoulder", "left_hip", "right_hip",
    "left_knee", "right_knee", "left_ankle", "right_ankle",
    "left_foot_index", "right_foot_index")]


def _player_core_bbox(row, w, h):
    """Scaled-px bbox of the player's core (head/torso/legs, NOT arms), padded 15%."""
    pts = row[_CORE_LMS, :2]
    pts = pts[~np.isnan(pts).any(axis=1)]
    if len(pts) < 4:
        return None
    x0, y0 = pts.min(axis=0); x1, y1 = pts.max(axis=0)
    px, py = (x1 - x0) * 0.15, (y1 - y0) * 0.15
    return (int((x0 - px) * w), int((y0 - py) * h),
            int((x1 + px) * w), int((y1 + py) * h))


def _wrist_and_reach(norm, f, w, h, wi, si):
    """Dominant wrist position + racket-reach radius in scaled px near frame f."""
    for df in (0, 1, -1, 2, -2, 3, -3):
        g = f + df
        if not (0 <= g < norm.shape[0]):
            continue
        wr, sh = norm[g, wi, :2], norm[g, si, :2]
        if np.isnan(wr).any():
            continue
        wx, wy = wr[0] * w, wr[1] * h
        if np.isnan(sh).any():
            reach = 0.12 * w
        else:
            arm = np.hypot((wr[0] - sh[0]) * w, (wr[1] - sh[1]) * h)
            reach = REACH_ARM_MULT * arm
        return (wx, wy), float(np.clip(reach, 30, 0.35 * w))
    return None, None


def _ball_area_band(norm, frames, w, h):
    """Expected ball blob area from player height (ball is ~4% of body height)."""
    hts = []
    for f in frames:
        if not (0 <= f < norm.shape[0]):
            continue
        r = norm[f]
        sy = (r[config.LM["left_shoulder"], 1] + r[config.LM["right_shoulder"], 1]) / 2
        ay = (r[config.LM["left_ankle"], 1] + r[config.LM["right_ankle"], 1]) / 2
        if not (np.isnan(sy) or np.isnan(ay)):
            hts.append(abs(ay - sy) * h / 0.72)   # shoulder-ankle ~72% of height
    if not hts:
        return 3.0, 400.0
    d = float(np.median(hts)) * 0.04              # ball diameter in px
    area = np.pi * (d / 2) ** 2
    return max(3.0, area * 0.25), min(1200.0, max(60.0, area * 6.0))


def _blobs(motion, amin, amax):
    """(x, y, area) centroids of ball-sized motion contours."""
    cnts, _ = cv2.findContours(motion, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out = []
    for c in cnts:
        a = cv2.contourArea(c)
        if not (amin <= a <= amax):
            continue
        (cx, cy), (bw, bh), _ang = cv2.minAreaRect(c)
        long_, short = max(bw, bh), max(1.0, min(bw, bh))
        if long_ / short > 4.0:                   # blur streaks OK, edges/limbs not
            continue
        out.append((float(cx), float(cy), float(a)))
    return out


def _link_tracks(frame_blobs):
    """Greedy nearest-neighbour linking with constant-velocity prediction."""
    tracks, done = [], []                          # track: {"pts": [(t,x,y)], "miss": int}
    for t, blobs in frame_blobs:
        used = set()
        for tr in tracks:
            (t0, x0, y0) = tr["pts"][-1]
            if len(tr["pts"]) >= 2:
                (t1, x1, y1) = tr["pts"][-2]
                vx, vy = (x0 - x1) / max(1, t0 - t1), (y0 - y1) / max(1, t0 - t1)
            else:
                vx = vy = 0.0
            dt = t - t0
            px, py = x0 + vx * dt, y0 + vy * dt
            gate = max(14.0, 2.5 * np.hypot(vx, vy) * dt + 8.0)
            best, bestd = None, gate
            for i, (bx, by, _a) in enumerate(blobs):
                if i in used:
                    continue
                d = np.hypot(bx - px, by - py)
                if d < bestd:
                    best, bestd = i, d
            if best is not None:
                bx, by, _a = blobs[best]
                tr["pts"].append((t, bx, by)); tr["miss"] = 0
                used.add(best)
            else:
                tr["miss"] += 1
        alive = []
        for tr in tracks:
            (done if tr["miss"] > 2 else alive).append(tr)
        tracks = [tr for tr in alive if tr["miss"] <= 2]
        for i, (bx, by, _a) in enumerate(blobs):
            if i not in used:
                tracks.append({"pts": [(t, bx, by)], "miss": 0})
    done.extend(tracks)
    return [tr["pts"] for tr in done if len(tr["pts"]) >= MIN_TRACK_PTS]


def _hit_from_track(pts, wrist_of, fps):
    """(contact_t, confidence, dmin, turn_deg, pts) if this track is a ball being struck.

    Three evidence patterns, all requiring the track to range well OUTSIDE racket
    reach at some point — racket/arm blobs live inside reach by definition, so the
    span requirement is what keeps a fast practice swing from verifying itself:
      A. the track turns >= TURN_DEG at its closest approach to the wrist
      B. an inbound track dies inside reach (contact occlusion kills the tracker)
      C. an outbound track is born inside reach (only the struck ball was seen)
    """
    arr = np.array(pts, dtype=float)               # [n, (t, x, y)]
    dts = np.diff(arr[:, 0]); steps = np.hypot(np.diff(arr[:, 1]), np.diff(arr[:, 2]))
    speed_pxs = float(np.median(steps / np.maximum(dts, 1))) * fps
    if speed_pxs < MIN_SPEED_PXS:
        return None
    dists, reaches = [], []
    for t, x, y in pts:
        wr = wrist_of(int(t))
        if wr[0] is None:
            dists.append(np.inf); reaches.append(np.inf)
        else:
            (wx, wy), reach = wr
            dists.append(np.hypot(x - wx, y - wy)); reaches.append(reach)
    finite = [d for d in dists if np.isfinite(d)]
    if not finite:
        return None
    k = int(np.argmin(dists))
    dmin, reach = dists[k], reaches[k]
    span = max(finite)

    def conf_of(track_len, closeness, turn_or_span):
        return (min(1.0, track_len / 10) * 0.40
                + min(1.0, turn_or_span) * 0.35
                + closeness * 0.25)

    # A. direction change at closest approach (both legs of the flight visible)
    if (np.isfinite(dmin) and dmin <= reach and 2 <= k <= len(pts) - 3
            and span >= 1.25 * reach):
        seg = min(3, k, len(pts) - 1 - k)
        v_in = (arr[k, 1:] - arr[k - seg, 1:]) / max(1e-6, arr[k, 0] - arr[k - seg, 0])
        v_out = (arr[k + seg, 1:] - arr[k, 1:]) / max(1e-6, arr[k + seg, 0] - arr[k, 0])
        ni, no = np.linalg.norm(v_in), np.linalg.norm(v_out)
        if ni > 1e-3 and no > 1e-3:
            turn = float(np.degrees(np.arccos(
                np.clip(np.dot(v_in, v_out) / (ni * no), -1, 1))))
            x_flip = (np.sign(v_in[0]) != np.sign(v_out[0])
                      and abs(v_in[0]) > 0.25 * ni and abs(v_out[0]) > 0.25 * no)
            if turn >= TURN_DEG or x_flip:
                conf = conf_of(len(pts), 1.0 - dmin / max(reach, 1e-6), turn / 120)
                return int(arr[k, 0]), float(conf), float(dmin), turn, pts

    # B. inbound flight ends inside reach — the strike occluded/deflected the ball
    d_end, r_end = dists[-1], reaches[-1]
    if (np.isfinite(d_end) and d_end <= r_end and len(pts) >= 5
            and span >= 1.6 * r_end
            and dists[-1] < dists[max(0, len(pts) - 4)]):     # actually approaching
        conf = conf_of(len(pts), 1.0 - d_end / max(r_end, 1e-6), span / (3 * r_end))
        return int(arr[-1, 0]) + 1, float(conf), float(d_end), 0.0, pts

    # C. only the outbound flight was seen, starting inside reach
    d0, r0 = dists[0], reaches[0]
    if (np.isfinite(d0) and d0 <= r0 and len(pts) >= 5
            and span >= 1.6 * r0
            and dists[min(3, len(pts) - 1)] > dists[0]):      # actually receding
        conf = conf_of(len(pts), 1.0 - d0 / max(r0, 1e-6), span / (3 * r0))
        return int(arr[0, 0]) - 1, float(conf), float(d0), 0.0, pts

    return None


def _annotate(frame, tracks, hit, wrist, reach, path):
    img = frame.copy()
    for pts in tracks:
        for (t0, x0, y0), (t1, x1, y1) in zip(pts, pts[1:]):
            cv2.line(img, (int(x0), int(y0)), (int(x1), int(y1)), (80, 80, 255), 1)
    if hit is not None:                            # winning ball track in green
        for (t0, x0, y0), (t1, x1, y1) in zip(hit[4], hit[4][1:]):
            cv2.line(img, (int(x0), int(y0)), (int(x1), int(y1)), (0, 255, 0), 2)
    if wrist is not None:
        cv2.circle(img, (int(wrist[0]), int(wrist[1])), int(reach), (0, 200, 255), 1)
    if hit is not None:
        cv2.putText(img, f"HIT conf={hit[1]:.2f} turn={hit[3]:.0f}", (10, 28),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
    else:
        cv2.putText(img, "NO BALL CONTACT", (10, 28),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
    cv2.imwrite(str(path), img)


def verify_hits(video_path: Path, strokes: list[dict], data: dict,
                annotate_dir: Path | None = None):
    """Vision ball-hit verdicts for every candidate stroke.

    Returns (strokes, gate). Each stroke gains contact_from_ball / ball_confidence,
    and a ball-verified stroke's contact_frame/time_s/window are re-anchored on the
    observed ball direction change. gate = {"active", "hits", "checked"}.
    """
    fps, n, W, H = data["fps"], data["n_frames"], data["width"], data["height"]
    norm = data["norm"]
    scale = min(1.0, PROC_W / W)
    w, h = int(W * scale), int(H * scale)
    wi = config.LM[f"{config.DOMINANT_HAND}_wrist"]
    si = config.LM[f"{config.DOMINANT_HAND}_shoulder"]
    win_frames = int(config.STROKE_WINDOW_SEC * fps)

    cap = cv2.VideoCapture(str(video_path))
    hits = 0
    try:
        for s in strokes:
            peak = s["peak_frame"]
            f0 = max(0, peak - int(PRE_S * fps))
            f1 = min(n - 1, peak + int(POST_S * fps))
            cap.set(cv2.CAP_PROP_POS_FRAMES, f0)
            frames = []
            for f in range(f0, f1 + 1):
                ok, img = cap.read()
                if not ok:
                    break
                if scale < 1.0:
                    img = cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA)
                frames.append((f, img))

            s["contact_from_ball"] = False
            s["ball_confidence"] = 0.0
            if len(frames) < 6:
                continue

            amin, amax = _ball_area_band(norm, [f for f, _ in frames], w, h)
            grays = [cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) for _, img in frames]
            frame_blobs = []
            for k in range(1, len(frames) - 1):
                d1 = cv2.absdiff(grays[k], grays[k - 1])
                d2 = cv2.absdiff(grays[k + 1], grays[k])
                motion = cv2.threshold(np.minimum(d1, d2), DIFF_THRESH, 255,
                                       cv2.THRESH_BINARY)[1]
                motion = cv2.dilate(motion, np.ones((3, 3), np.uint8))
                f = frames[k][0]
                if 0 <= f < norm.shape[0]:
                    box = _player_core_bbox(norm[f], w, h)
                    if box:
                        x0, y0, x1, y1 = box
                        motion[max(0, y0):max(0, y1), max(0, x0):max(0, x1)] = 0
                frame_blobs.append((f, _blobs(motion, amin, amax)))

            tracks = _link_tracks(frame_blobs)
            wrist_of = lambda f: _wrist_and_reach(norm, f, w, h, wi, si)  # noqa: E731
            best = None
            for pts in tracks:
                hit = _hit_from_track(pts, wrist_of, fps)
                if hit and hit[1] >= MIN_CONFIDENCE and (best is None or hit[1] > best[1]):
                    best = hit
            if best:
                hits += 1
                contact = int(np.clip(best[0], 0, n - 1))
                s["contact_from_ball"] = True
                s["ball_confidence"] = round(best[1], 2)
                s["contact_frame"] = contact
                s["time_s"] = round(contact / fps, 2)
                s["window"] = (max(0, contact - win_frames), min(n - 1, contact + win_frames))

            if annotate_dir is not None:
                annotate_dir.mkdir(parents=True, exist_ok=True)
                mid = frames[len(frames) // 2][1]
                wr = wrist_of(s["contact_frame"])
                _annotate(mid, tracks, best,
                          wr[0] if wr[0] is not None else None,
                          wr[1] if wr[1] is not None else 40,
                          annotate_dir / f"stroke_{s['stroke_no']:03d}"
                                         f"_{'hit' if best else 'nohit'}.jpg")
    finally:
        cap.release()

    checked = len(strokes)
    if checked >= GATE_MIN_STROKES:
        active = hits / checked >= GATE_MIN_HIT_FRAC
    else:
        active = hits >= 1
    print(f"[ballgate] {hits}/{checked} candidate swings ball-verified "
          f"-> gate {'ACTIVE' if active else 'OFF (ball not trackable)'}")
    return strokes, {"active": active, "hits": hits, "checked": checked}
