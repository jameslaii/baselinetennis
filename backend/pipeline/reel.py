"""Slow-mo coach reel — self-contained port of tennis-coach handpath_reel.render().

Vendored verbatim where possible: the rolling per-swing loop, _icon/_panel/_stamp/
_report_card (handpath_reel.py), _fixed_crop/_to_canvas/_draw_skel/_player_center_bbox
(swing_study.py), CONNECTIONS (annotate.py) and _to_h264 (highlight_reel.py) — with no
pandas/annotate/highlight_reel imports so the container stays lean. Text is cv2 Hershey
only (headless-safe). analyse() comes from .grading (the one source of metric truth);
its verdict tags are the strings "good"/"ok"/"bad", mapped to BGR here for drawing.
"""
import shutil
import subprocess
from pathlib import Path

import cv2
import numpy as np

from . import config
from . import extract_pose
from . import grading

CW, CH = 720, 1280           # portrait 9:16 canvas
PANEL_H = 330
REEL_MAX_SWINGS = 12

GREEN = (70, 200, 70); YELLOW = (40, 200, 235); RED = (50, 50, 240)
WHITE = (245, 245, 245); GREY = (160, 160, 160); ORANGE = (40, 120, 255); CYAN = (230, 200, 80)
COL = {"good": GREEN, "ok": YELLOW, "bad": RED}   # grading.py tag string -> BGR
LM = config.LM

# set by render() per job (one job per container, as with the analysis worker)
STROKE = "forehand"
REF_NAME = "Alcaraz"
REF_KNEE = 135.0
OUT_FPS = 30


# ---------------------------------------------------------------------------
# framing helpers (swing_study.py)
# ---------------------------------------------------------------------------
def _player_center_bbox(row, W, H):
    vis = row[:, 3] > 0.3
    pts = row[vis] if vis.sum() >= 6 else row
    xs, ys = pts[:, 0] * W, pts[:, 1] * H
    cx = float(np.mean([row[LM["left_hip"]][0], row[LM["right_hip"]][0],
                        row[LM["left_shoulder"]][0], row[LM["right_shoulder"]][0]]) * W)
    cy = float(np.mean([row[LM["left_hip"]][1], row[LM["right_hip"]][1],
                        row[LM["left_shoulder"]][1], row[LM["right_shoulder"]][1]]) * H)
    return cx, cy, xs.min(), ys.min(), xs.max(), ys.max()


def _fixed_crop(rows, frames_idx, W, H):
    """One stable 9:16 crop per hit: covers the range, keeps the player large/steady."""
    cxs, cys, h_list, x0s, y0s, x1s, y1s = [], [], [], [], [], [], []
    for r in rows:
        if np.isnan(r).any():
            continue
        cx, cy, xa, ya, xb, yb = _player_center_bbox(r, W, H)
        cxs.append(cx); cys.append(cy); h_list.append(yb - ya)
        x0s.append(xa); y0s.append(ya); x1s.append(xb); y1s.append(yb)
    if not cxs:
        return 0, 0, W, H, CW / W
    med_h = float(np.median(h_list))
    span_y = max(y1s) - min(y0s); span_x = max(x1s) - min(x0s)
    crop_h = min(H, max(med_h * 2.8, span_y * 1.15, 240))
    crop_w = crop_h * CW / CH
    if crop_w > W:
        crop_w = W; crop_h = crop_w * CH / CW
    if crop_w < span_x * 1.05:
        crop_w = min(W, span_x * 1.05); crop_h = min(H, crop_w * CH / CW)
        crop_w = crop_h * CW / CH
    cx = float(np.median(cxs)); cy = float(np.median(cys))
    x0 = int(min(max(0, cx - crop_w / 2), W - crop_w))
    y0 = int(min(max(0, cy - crop_h / 2), H - crop_h))
    return x0, y0, int(crop_w), int(crop_h), CW / crop_w


def _to_canvas(px, py, ox, oy, scale):
    return int((px - ox) * scale), int((py - oy) * scale)


def _draw_skel(canvas, row, W, H, ox, oy, scale):
    pts = [_to_canvas(row[i, 0] * W, row[i, 1] * H, ox, oy, scale) for i in range(33)]
    for a, b in grading._RAW_CONNECTIONS:
        if row[a, 3] > 0.3 and row[b, 3] > 0.3:
            cv2.line(canvas, pts[a], pts[b], GREEN, 2, cv2.LINE_AA)
    for i in range(33):
        if row[i, 3] > 0.3:
            cv2.circle(canvas, pts[i], 3, (0, 200, 255), -1, cv2.LINE_AA)
    return pts


# ---------------------------------------------------------------------------
# overlays (handpath_reel.py, colors adapted to grading's string tags)
# ---------------------------------------------------------------------------
def _icon(canvas, cx, cy, col):
    if tuple(col) == tuple(GREEN):
        cv2.line(canvas, (cx-9, cy+1), (cx-2, cy+8), GREEN, 3, cv2.LINE_AA)
        cv2.line(canvas, (cx-2, cy+8), (cx+10, cy-9), GREEN, 3, cv2.LINE_AA)
    elif tuple(col) == tuple(RED):
        cv2.line(canvas, (cx-8, cy-8), (cx+8, cy+8), RED, 3, cv2.LINE_AA)
        cv2.line(canvas, (cx-8, cy+8), (cx+8, cy-8), RED, 3, cv2.LINE_AA)
    else:
        cv2.line(canvas, (cx-8, cy), (cx+8, cy), YELLOW, 4, cv2.LINE_AA)


def _panel(canvas, fh_no, a, revealed, tally):
    cv2.rectangle(canvas, (0, 0), (CW, PANEL_H), (26, 26, 30), -1)
    cv2.putText(canvas, f"{STROKE.upper()} ANALYSIS", (16, 32), cv2.FONT_HERSHEY_DUPLEX, 0.8, ORANGE, 1, cv2.LINE_AA)
    cv2.putText(canvas, f"#{fh_no}", (CW - 80, 32), cv2.FONT_HERSHEY_DUPLEX, 0.8, WHITE, 1, cv2.LINE_AA)
    if not revealed:
        cv2.putText(canvas, "reading swing...", (16, 78), cv2.FONT_HERSHEY_SIMPLEX, 0.7, GREY, 1, cv2.LINE_AA)
    else:
        rows = [("RACKET DROP", a["drp"][0], COL[a["drp"][1]]),
                ("LOW-TO-HIGH", a["lth"][0], COL[a["lth"][1]]),
                ("FINISH", a["fin"][0], COL[a["fin"][1]]),
                ("CONTACT HT", a["cverd"][0], COL[a["cverd"][1]]),
                ("LEG LOAD", a["lverd"][0], COL[a["lverd"][1]])]
        y = 60
        for name, lab, col in rows:
            _icon(canvas, 26, y - 5, col)
            cv2.putText(canvas, name, (48, y), cv2.FONT_HERSHEY_SIMPLEX, 0.56, WHITE, 1, cv2.LINE_AA)
            cv2.putText(canvas, lab, (270, y), cv2.FONT_HERSHEY_DUPLEX, 0.56, col, 1, cv2.LINE_AA)
            y += 30
        ov, otag = a["overall"]
        ocol = COL[otag]
        cv2.rectangle(canvas, (12, y), (CW - 12, y + 44), ocol, -1)
        cv2.putText(canvas, ov, (24, y + 31), cv2.FONT_HERSHEY_DUPLEX, 0.8, (18, 18, 18), 2, cv2.LINE_AA)
        cv2.putText(canvas, a["note"], (16, y + 70), cv2.FONT_HERSHEY_SIMPLEX, 0.52, (200, 210, 235), 1, cv2.LINE_AA)
    cv2.putText(canvas, f"Good hand paths: {tally}", (16, PANEL_H - 14), cv2.FONT_HERSHEY_SIMPLEX,
                0.58, GREEN if tally and tally[0] != "0" else GREY, 1, cv2.LINE_AA)


def _stamp(canvas, a):
    """Big at-a-glance GOOD/OK/BAD stamp on the video as the reel rolls."""
    tag, col = a["tag"], COL[a["tagcol"]]
    bw, bh = 196, 74
    x0, y0 = CW - bw - 16, PANEL_H + 14
    cv2.rectangle(canvas, (x0, y0), (x0 + bw, y0 + bh), (15, 15, 15), -1)
    cv2.rectangle(canvas, (x0, y0), (x0 + bw, y0 + bh), col, 3)
    cv2.putText(canvas, "HAND PATH", (x0 + 14, y0 + 20), cv2.FONT_HERSHEY_SIMPLEX, 0.45, WHITE, 1, cv2.LINE_AA)
    (tw, th), _ = cv2.getTextSize(tag, cv2.FONT_HERSHEY_DUPLEX, 1.5, 3)
    cv2.putText(canvas, tag, (x0 + (bw - tw) // 2, y0 + bh - 16), cv2.FONT_HERSHEY_DUPLEX, 1.5, col, 3, cv2.LINE_AA)


def _report_card(writer, results, fps):
    n = len(results)
    good = sum(r["good"] for r in results)
    flat = sum(r["flat_finish"] for r in results)
    lows = sum(1 for r in results if r["cverd"][0] == "LOW")
    highs = sum(1 for r in results if r["cverd"][0] == "HIGH/LATE")
    goodc = sum(r["good_contact"] for r in results)
    loaded = sum(r["loaded"] for r in results)
    avg_knee = np.nanmean([r["knee"] for r in results])
    shallow = sum(1 for r in results if r["drp"][0] == "SHALLOW")
    issues = []
    if flat:
        issues.append((flat, "Finish high & across EVERY ball",
                       f"(you flatten on {flat}/{n})"))
    if shallow:
        issues.append((shallow, "Drop the racket below the ball",
                       f"before swinging up ({shallow}/{n} shallow)"))
    if n - goodc:
        issues.append((n - goodc, "Steadier contact - meet it waist",
                       f"to shoulder ({lows} low, {highs} high/late)"))
    if n - loaded:
        issues.append((n - loaded, "Load the legs - sit lower",
                       f"before the hit ({n - loaded}/{n} stand tall)"))
    issues.sort(key=lambda t: -t[0])
    work = []
    for i, (_, main, sub) in enumerate(issues[:2], 1):
        work += [(f"{i}) {main}", CYAN, 0.6), (f"   {sub}", GREY, 0.52)]
    if not work:
        work = [("Nothing glaring - keep grooving", CYAN, 0.6),
                ("   this swing under pressure", GREY, 0.52)]
    closing = (["Most swings are tour-shaped -", "keep this shape under match pace."]
               if good >= n * 0.6 else
               ["Your best swings are tour-shaped -", "now make every one look like that."])
    lines = [
        (f"{STROKE.upper()} REPORT CARD", ORANGE, 0.95),
        ("", WHITE, 0.4),
        (f"Good hand path:   {good}/{n}", GREEN if good >= n*0.6 else YELLOW, 0.72),
        (f"Strike zone:      {goodc}/{n} in zone  ({lows} low, {highs} high)",
         GREEN if goodc >= n*0.6 else YELLOW, 0.62),
        (f"Leg load:         {loaded}/{n} loaded  (avg knee {avg_knee:.0f} vs {REF_NAME} {REF_KNEE:.0f})",
         GREEN if loaded >= n*0.6 else YELLOW, 0.62),
        ("", WHITE, 0.4),
        ("WORK ON:", WHITE, 0.7),
        *work,
        ("", WHITE, 0.4),
        *[(t, GREEN, 0.58) for t in closing],
    ]
    for _ in range(int(4.5 * OUT_FPS)):
        card = np.full((CH, CW, 3), (22, 22, 26), np.uint8)
        y = 120
        for txt, col, sc in lines:
            if txt:
                cv2.putText(card, txt, (40, y), cv2.FONT_HERSHEY_DUPLEX, sc, col, 1, cv2.LINE_AA)
            y += int(44 * sc + 18)
        writer.write(card)


def _to_h264(path: Path, fps: float):
    """Re-encode the bulky mp4v output to small, universally-playable H.264."""
    if shutil.which("ffmpeg") is None:
        print("[reel] ffmpeg not found; leaving mp4v file as-is.")
        return
    tmp = path.with_name("_h264_tmp.mp4")
    try:
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-r", f"{fps:.4f}", "-i", str(path),
                        "-c:v", "libx264", "-crf", "21", "-pix_fmt", "yuv420p",
                        "-movflags", "+faststart", str(tmp)], check=True)
        tmp.replace(path)
        print(f"[reel] re-encoded to H.264 ({path.stat().st_size // (1024*1024)} MB).")
    except (subprocess.CalledProcessError, OSError) as e:
        if tmp.exists():
            tmp.unlink()
        print(f"[reel] H.264 re-encode skipped ({e}); kept mp4v file.")


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
def pick_reel_contacts(contacts: list[int], best_idx: int | None,
                       max_swings: int = REEL_MAX_SWINGS) -> list[int]:
    """Evenly time-spaced subset for the reel, always including the best swing."""
    if len(contacts) <= max_swings:
        return contacts
    idxs = set(np.linspace(0, len(contacts) - 1, max_swings).round().astype(int).tolist())
    if best_idx is not None and 0 <= best_idx < len(contacts):
        idxs.add(best_idx)
        while len(idxs) > max_swings:
            spaced = sorted(idxs - {best_idx})
            idxs.discard(spaced[len(spaced) // 2])
    return [contacts[i] for i in sorted(idxs)]


def render(video_path: Path, data: dict, contacts: list[int], out_path: Path,
           stroke: str = "forehand", ref_name: str = "Alcaraz", ref_knee: float = 135.0,
           out_fps: int | None = None, cache_tag: str = "heavy", progress_cb=None):
    """Render the rolling slow-mo reel. `data` = extract_pose session dict for the
    normalized video (npz cache must exist); reuses the analysis job's heavy pose
    cache when it covers the windows (cache_tag)."""
    global STROKE, REF_NAME, REF_KNEE, OUT_FPS
    video_path, out_path = Path(video_path), Path(out_path)
    sn = data["norm"]; fps = float(data["fps"]); W = int(data["width"]); H = int(data["height"])
    STROKE, REF_NAME, REF_KNEE = stroke, ref_name, float(ref_knee)
    OUT_FPS = out_fps or max(24, round(fps / 2))          # 0.5x slow-mo by default
    grading.REF_KNEE = float(ref_knee)
    grading.REF_NAME = ref_name
    wi = LM[f"{config.DOMINANT_HAND}_wrist"]

    windows = [(max(0, c - int(grading.WIN_SEC * fps)),
                min(sn.shape[0] - 1, c + int(grading.WIN_SEC * fps))) for c in contacts]
    heavy = extract_pose.extract_windows(video_path, windows, seed_norm=sn,
                                         variant="heavy", cache_tag=cache_tag)
    hf = {int(f): i for i, f in enumerate(heavy["frames"])}
    nn = heavy["norm"]; ww = heavy["world"]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(str(out_path), cv2.VideoWriter_fourcc(*"mp4v"), OUT_FPS, (CW, CH))
    cap = cv2.VideoCapture(str(video_path))

    results = []
    good_count = 0
    for n, c in enumerate(contacts, 1):
        try:
            a = grading.analyse(c, hf, nn, ww, W, H, fps)
        except (ValueError, KeyError, IndexError):
            continue
        win = a["win"]
        rows = [nn[hf[f]] for f in win]
        ox, oy, cw, ch, scale = _fixed_crop(rows, win, W, H)
        tally = f"{good_count}/{n-1}" if n > 1 else "0/0"
        trail = []; low_px = fin_px = None
        cap.set(cv2.CAP_PROP_POS_FRAMES, win[0])
        for f in win:
            ok, frame = cap.read()
            if not ok:
                break
            row = nn[hf[f]]
            canvas = cv2.resize(frame[oy:oy+ch, ox:ox+cw], (CW, CH))
            if not np.isnan(row).any():
                _draw_skel(canvas, row, W, H, ox, oy, scale)
                trail.append((row[wi, 0]*W, row[wi, 1]*H))
            if f == a["low_f"]:
                low_px = (nn[hf[f]][wi, 0]*W, nn[hf[f]][wi, 1]*H)
            if f == a["fin_f"]:
                fin_px = (nn[hf[f]][wi, 0]*W, nn[hf[f]][wi, 1]*H)
            for j in range(1, len(trail)):
                al = j/len(trail)
                cv2.line(canvas, _to_canvas(*trail[j-1], ox, oy, scale),
                         _to_canvas(*trail[j], ox, oy, scale),
                         (int(CYAN[0]*al), int(CYAN[1]*al), int(80+175*al)),
                         max(1, int(2+3*al)), cv2.LINE_AA)
            if trail:
                cv2.circle(canvas, _to_canvas(*trail[-1], ox, oy, scale), 6, CYAN, -1, cv2.LINE_AA)
            revealed = f >= a["contact"]
            if low_px is not None and f >= a["low_f"]:
                p = _to_canvas(*low_px, ox, oy, scale)
                cv2.circle(canvas, p, 9, RED, 2, cv2.LINE_AA)
                cv2.putText(canvas, "LOW", (p[0]+10, p[1]+4), cv2.FONT_HERSHEY_SIMPLEX, 0.5, RED, 1, cv2.LINE_AA)
            if fin_px is not None and revealed:
                p = _to_canvas(*fin_px, ox, oy, scale)
                cv2.circle(canvas, p, 9, GREEN, 2, cv2.LINE_AA)
                cv2.putText(canvas, "FINISH", (p[0]+10, p[1]+4), cv2.FONT_HERSHEY_SIMPLEX, 0.5, GREEN, 1, cv2.LINE_AA)
            _panel(canvas, n, a, revealed, tally)
            if revealed:
                _stamp(canvas, a)
            writer.write(canvas)
        good_count += int(a["good"]); results.append(a)
        if progress_cb:
            progress_cb(n, len(contacts))

    if results:
        _report_card(writer, results, fps)
    cap.release(); writer.release()
    print(f"[reel] {good_count}/{len(contacts)} good hand paths -> {out_path}")
    _to_h264(out_path, OUT_FPS)
    return {"swings": len(results), "good": good_count, "out_fps": OUT_FPS}
