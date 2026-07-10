"""Local smoke test for the vendored grading pipeline — no mediapipe needed.

Runs against the existing pose caches in C:\\Users\\james\\tennis-coach\\data:

  1. PARITY  — Iris's 10 hand-verified forehand contacts (clip 20260706_212034) graded
     on the same fh_heavy cache the desktop coach reel used, SNAP_SEC=0.05, ref eala.
     Known desktop results: good hand paths 5/10, loaded 7/10, avg knee@contact ~130,
     zone 7/10 (0 low, 3 high).
  2. AUTO    — the full service path (detect_strokes -> wing filter -> rank -> grade ->
     aggregate) on the "james hitting" session cache (right-handed, ref alcaraz).

Run with the tennis-coach venv python (has numpy/scipy/librosa):
  C:\\Users\\james\\tennis-coach\\.venv\\Scripts\\python.exe backend\\smoke_test.py
"""
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipeline import config, grading, segment  # noqa: E402

TC_DATA = Path(r"C:\Users\james\tennis-coach\data")

IRIS_FH_CONTACTS = [2093, 2405, 7146, 9239, 9887, 10616, 11158, 12553, 13661, 14765]


def load_session(stem):
    z = np.load(TC_DATA / f"{stem}_landmarks.npz", allow_pickle=True)
    return {"video": str(z["video"]), "fps": float(z["fps"]), "width": int(z["width"]),
            "height": int(z["height"]), "n_frames": int(z["n_frames"]),
            "norm": z["norm"], "world": z["world"]}


def test_parity_iris():
    print("=" * 70)
    print("TEST 1: parity — Iris FH, hand-verified contacts, heavy cache, ref eala")
    sess = load_session("20260706_212034")
    W, H, fps = sess["width"], sess["height"], sess["fps"]
    z = np.load(TC_DATA / "20260706_212034_fh_heavy.npz", allow_pickle=True)
    hf = {int(f): i for i, f in enumerate(z["frames"])}
    nn, ww = z["norm"], z["world"]

    config.DOMINANT_HAND = "left"
    grading.set_reference("eala")
    grading.SNAP_SEC = 0.05
    try:
        results = [grading.analyse(c, hf, nn, ww, W, H, fps) for c in IRIS_FH_CONTACTS]
    finally:
        grading.SNAP_SEC = 0.3

    good = sum(r["good"] for r in results)
    loaded = sum(r["loaded"] for r in results)
    zone = sum(r["good_contact"] for r in results)
    lows = sum(1 for r in results if r["cverd"][0] == "LOW")
    highs = sum(1 for r in results if r["cverd"][0] == "HIGH/LATE")
    knees = [r["knee"] for r in results if not np.isnan(r["knee"])]
    avg_knee = np.mean(knees)
    print(f"  good paths {good}/10 (expect 5)  loaded {loaded}/10 (expect 7)")
    print(f"  zone {zone}/10 (expect 7: 0 low={lows}, 3 high={highs})  avg knee {avg_knee:.1f} (expect ~130)")

    agg = grading.aggregate(results, grading.REF_NAME, grading.REF_KNEE)
    print(f"  aggregate verdict: {agg['verdict']}  priority: {agg['priority']['key']}")
    ok = (good == 5 and loaded == 7 and zone == 7 and abs(avg_knee - 130) < 3)
    print(f"  PARITY {'PASS' if ok else 'MISMATCH — investigate before deploying'}")
    return ok


def test_auto_james():
    print("=" * 70)
    print("TEST 2: auto service path — 'james hitting' session cache, ref alcaraz")
    sess = load_session("james hitting")
    W, H, fps = sess["width"], sess["height"], sess["fps"]
    config.DOMINANT_HAND = "right"
    grading.set_reference("alcaraz", "Carlos Alcaraz")

    # video file may be offline; detect_strokes only needs it for audio (fails soft)
    strokes = segment.detect_strokes(Path(r"D:\tennis vid\james hitting.mp4"), data=sess)
    print(f"  strokes detected: {len(strokes)} (desktop found 148)")

    selected, stats = grading.rank_candidates(
        strokes, sess["norm"], sess["world"], W, H, fps, "forehand")
    print(f"  wing counts: {stats['counts']}  graded {stats['graded']}  dropped {stats['dropped']}")
    assert len(selected) > 0, "no swings selected"

    results = [a for _, a in selected]
    agg = grading.aggregate(results, grading.REF_NAME, grading.REF_KNEE)
    hf = {f: f for f in range(sess["norm"].shape[0])}
    paths = [grading.hand_path_points(a, hf, sess["norm"], W, H) for a in results]
    n_pts = [len(p["points"]) for p in paths]
    knees = [r["knee"] for r in results if not np.isnan(r["knee"])]
    print(f"  top {agg['n']} graded: good paths {agg['good_hand_path']}, "
          f"avg knee {np.mean(knees):.1f}, verdict {agg['verdict']['label']}")
    print(f"  priority: {agg['priority']['key']}  hand-path points/swing: {n_pts}")
    print(f"  tiles: {json.dumps(agg['tiles'], indent=None)}")
    sane = (5 <= agg["n"] <= 10 and all(100 < k < 185 for k in knees)
            and all(np - 1 >= 10 for np in n_pts))
    print(f"  AUTO PATH {'PASS' if sane else 'FAIL — values out of sane range'}")
    return sane


if __name__ == "__main__":
    ok1 = test_parity_iris()
    ok2 = test_auto_james()
    print("=" * 70)
    print("SMOKE TEST:", "ALL PASS" if (ok1 and ok2) else "FAILURES — see above")
    sys.exit(0 if (ok1 and ok2) else 1)
