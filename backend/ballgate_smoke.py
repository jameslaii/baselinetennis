"""Local smoke for pipeline.ballgate — needs the real video + cached poses, no mediapipe.

Runs verify_hits over the full "james hitting" session (D:\\tennis vid\\james hitting.mp4,
pose cache in C:\\Users\\james\\tennis-coach\\data) and reports:
  * hits/candidates + gate decision (the clip is a real hitting session, so the
    gate must come out ACTIVE and most rally swings must verify)
  * how far ball-anchored contacts moved vs the raw wrist-speed peaks
  * annotated JPEGs (tracks + reach circle + verdict) for eyeballing, in
    the directory passed as argv[1] (default: alongside this file, ballgate_annot/)

Run with the tennis-coach venv python:
  C:\\Users\\james\\tennis-coach\\.venv\\Scripts\\python.exe backend\\ballgate_smoke.py <annot_dir>
"""
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipeline import ballgate, config, segment  # noqa: E402

TC_DATA = Path(r"C:\Users\james\tennis-coach\data")
VIDEO = Path(r"D:\tennis vid\james hitting.mp4")


def main():
    annot = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "ballgate_annot"
    z = np.load(TC_DATA / "james hitting_landmarks.npz", allow_pickle=True)
    data = {"fps": float(z["fps"]), "width": int(z["width"]), "height": int(z["height"]),
            "n_frames": int(z["n_frames"]), "norm": z["norm"], "world": z["world"]}
    print(f"video {data['width']}x{data['height']} @ {data['fps']:.1f} fps, "
          f"{data['n_frames']} frames ({data['n_frames']/data['fps']:.0f}s)")
    config.DOMINANT_HAND = "right"

    strokes = segment.detect_strokes(VIDEO, data=data)
    t0 = time.time()
    strokes, gate = ballgate.verify_hits(VIDEO, strokes, data, annotate_dir=annot)
    dt = time.time() - t0
    print(f"verify_hits took {dt:.0f}s for {gate['checked']} windows "
          f"({dt/max(1,gate['checked']):.2f}s/window)")

    hits = [s for s in strokes if s["contact_from_ball"]]
    shifts = [s["contact_frame"] - s["peak_frame"] for s in hits]
    confs = [s["ball_confidence"] for s in hits]
    print(f"hits {gate['hits']}/{gate['checked']}  gate active={gate['active']}")
    if hits:
        print(f"contact shift vs peak (frames): median {np.median(shifts):.0f}, "
              f"p10 {np.percentile(shifts,10):.0f}, p90 {np.percentile(shifts,90):.0f}")
        print(f"confidence: median {np.median(confs):.2f}, min {min(confs):.2f}, max {max(confs):.2f}")
    print(f"annotated frames -> {annot}")

    ok = gate["active"] and gate["hits"] >= 0.4 * gate["checked"]
    print("BALLGATE SMOKE:", "PASS" if ok else "FAIL — inspect annotated frames")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
