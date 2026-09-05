"""Baseline — Modal backend.

One Modal app with three functions:
  * api         — FastAPI: upload a clip (invite-code gated), poll job status/results,
                  request/serve the slow-mo coach reel.
  * analyze     — worker: ffmpeg normalize -> pose extraction -> stroke detection ->
                  wing classify -> grade EVERY matching swing (evenly time-spaced cap
                  of 30) -> heavy re-grade -> results JSON on the volume.
  * render_reel — worker: on-demand slow-mo handpath reel (reuses the analysis job's
                  heavy pose cache), served via a per-job token.

Deploy:  modal deploy backend/modal_app.py
Secret:  modal secret create baseline-invite INVITE_CODE=<code>
"""
import hmac
import json
import os
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import modal

app = modal.App("baseline")

MAX_BYTES = 500 * 1024 * 1024      # hard upload cap
MAX_DURATION_S = 250               # "1-4 minute clips" + slack
DAILY_JOB_CAP = 15
BENCHMARKS = {"alcaraz": "Carlos Alcaraz", "eala": "Alex Eala"}

VOL_PATH = Path("/data")
vol = modal.Volume.from_name("baseline-videos", create_if_missing=True)
jobs = modal.Dict.from_name("baseline-jobs", create_if_missing=True)
invite_secret = modal.Secret.from_name("baseline-invite")


def download_models():
    """Bake both pose models into the image at build time."""
    import urllib.request
    tmpl = ("https://storage.googleapis.com/mediapipe-models/pose_landmarker/"
            "pose_landmarker_{v}/float16/latest/pose_landmarker_{v}.task")
    os.makedirs("/root/models", exist_ok=True)
    for v in ("full", "heavy"):
        dest = f"/root/models/pose_landmarker_{v}.task"
        print("downloading", dest)
        urllib.request.urlretrieve(tmpl.format(v=v), dest)


pipeline_image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("ffmpeg", "libgl1", "libglib2.0-0", "libgles2", "libegl1")
    .pip_install("mediapipe==0.10.35", "scipy~=1.14")
    .run_function(download_models)
    .env({"BASELINE_MODELS_DIR": "/root/models"})
    .add_local_dir(Path(__file__).parent / "pipeline", "/root/pipeline")
)

web_image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("ffmpeg")                       # ffprobe for the early duration check
    .pip_install("fastapi[standard]~=0.115")
)


# ---------------------------------------------------------------------------
# helpers shared by both functions
# ---------------------------------------------------------------------------
def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _job_dir(job_id: str) -> Path:
    return VOL_PATH / "jobs" / job_id


def _set_status(job_id: str, **updates):
    entry = jobs.get(job_id) or {}
    entry.update(updates)
    jobs.put(job_id, entry)


def _ffprobe_duration(path: Path) -> float | None:
    """Length of the VIDEO STREAM — deliberately not the container duration.

    iPhone .MOV files carry Apple `mebx` timed-metadata tracks (and an `apac`
    spatial-audio track) beside the video. Trimming a clip leaves those metadata
    tracks holding their ORIGINAL timestamps, and `format=duration` is the max
    across every track — so a 20s trimmed MOV probes as 562s and gets bounced as
    "too long". Since we ask users to trim to fit the size cap, that made every
    trimmed phone clip unuploadable.
    """
    def probe(args: list[str]) -> dict[str, str]:
        out = subprocess.run(["ffprobe", "-v", "error", *args, "-of",
                              "default=noprint_wrappers=1:nokey=0", str(path)],
                             capture_output=True, text=True, timeout=60)
        fields = {}
        for line in out.stdout.splitlines():
            key, _, val = line.partition("=")
            val = val.strip()
            if val and val != "N/A":
                fields[key.strip()] = val
        return fields

    try:
        v = probe(["-select_streams", "v:0", "-show_entries",
                   "stream=duration,nb_frames,r_frame_rate"])
        if "duration" in v:
            d = float(v["duration"])
            if d > 0:
                return d
        # Remuxed streams often carry no duration header — derive it from frames.
        if "nb_frames" in v and "/" in v.get("r_frame_rate", ""):
            num, den = v["r_frame_rate"].split("/")
            fps = float(num) / float(den) if float(den) else 0.0
            if fps > 0:
                d = float(v["nb_frames"]) / fps
                if d > 0:
                    return d
        # Last resort: container duration. Right for everything except the
        # Apple-metadata case above, and better than refusing the upload.
        f = probe(["-show_entries", "format=duration"])
        return float(f["duration"]) if "duration" in f else None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
@app.function(image=web_image, volumes={str(VOL_PATH): vol},
              secrets=[invite_secret], timeout=600)
@modal.concurrent(max_inputs=10)
@modal.asgi_app()
def api():
    from fastapi import FastAPI, Header, HTTPException, Query, Request
    from fastapi.middleware.cors import CORSMiddleware

    web = FastAPI(title="Baseline API")
    web.add_middleware(
        CORSMiddleware,
        allow_origins=["https://baselinetennis.vercel.app",
                       "https://baseline-mockup.vercel.app",
                       "http://localhost:8000", "http://127.0.0.1:8000"],
        allow_origin_regex=r"https://baseline-mockup[a-z0-9\-]*\.vercel\.app",
        allow_methods=["GET", "POST"],
        allow_headers=["X-Invite-Code", "Content-Type"],
    )

    def check_invite(code: str | None):
        expected = os.environ["INVITE_CODE"]
        if not code or not hmac.compare_digest(code.strip(), expected):
            raise HTTPException(401, detail={"code": "bad_invite",
                                             "message": "Invite code missing or wrong."})

    @web.get("/api/health")
    def health():
        return {"ok": True, "time": _utc_now()}

    @web.post("/api/jobs", status_code=202)
    async def create_job(
        request: Request,
        handedness: str = Query(...),
        stroke: str = Query(...),
        benchmark: str = Query(...),
        x_invite_code: str | None = Header(default=None),
    ):
        check_invite(x_invite_code)
        if handedness not in ("left", "right"):
            raise HTTPException(422, detail={"code": "bad_param", "message": "handedness must be left or right"})
        if stroke not in ("forehand", "backhand"):
            raise HTTPException(422, detail={"code": "bad_param", "message": "stroke must be forehand or backhand"})
        if benchmark not in BENCHMARKS:
            raise HTTPException(422, detail={"code": "bad_param", "message": "benchmark must be alcaraz or eala"})

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        quota_key = f"quota:{today}"
        if (jobs.get(quota_key) or 0) >= DAILY_JOB_CAP:
            raise HTTPException(429, detail={"code": "daily_cap",
                                             "message": "Today's analysis quota is used up - try tomorrow."})

        clen = request.headers.get("content-length")
        if clen and int(clen) > MAX_BYTES:
            raise HTTPException(413, detail={"code": "too_big",
                                             "message": "Video is over 500 MB. Export at 1080p and retry."})

        job_id = uuid.uuid4().hex
        jobdir = _job_dir(job_id)
        jobdir.mkdir(parents=True, exist_ok=True)
        upload = jobdir / "upload.bin"
        written = 0
        try:
            with open(upload, "wb") as fh:
                async for chunk in request.stream():
                    written += len(chunk)
                    if written > MAX_BYTES:
                        raise HTTPException(413, detail={"code": "too_big",
                                                         "message": "Video is over 500 MB. Export at 1080p and retry."})
                    fh.write(chunk)
        except HTTPException:
            shutil.rmtree(jobdir, ignore_errors=True)
            raise
        if written == 0:
            shutil.rmtree(jobdir, ignore_errors=True)
            raise HTTPException(400, detail={"code": "bad_video", "message": "Empty upload."})

        duration = _ffprobe_duration(upload)
        if duration is None:
            shutil.rmtree(jobdir, ignore_errors=True)
            raise HTTPException(400, detail={"code": "bad_video",
                                             "message": "Could not read that file as a video."})
        if duration > MAX_DURATION_S:
            shutil.rmtree(jobdir, ignore_errors=True)
            raise HTTPException(400, detail={"code": "too_long",
                                             "message": f"Clip is {duration:.0f}s - trim it to 4 minutes or less."})

        jobs.put(quota_key, (jobs.get(quota_key) or 0) + 1)
        params = {"handedness": handedness, "stroke": stroke, "benchmark": benchmark}
        jobs.put(job_id, {"status": "queued", "stage": "queued", "percent": 1,
                          "params": params, "created_at": _utc_now()})
        vol.commit()
        analyze.spawn(job_id, params)
        return {"job_id": job_id}

    @web.get("/api/jobs/{job_id}")
    def get_job(job_id: str, x_invite_code: str | None = Header(default=None)):
        check_invite(x_invite_code)
        entry = jobs.get(job_id)
        if entry is None:
            raise HTTPException(404, detail={"code": "not_found", "message": "No such job."})
        entry.pop("reel_token", None)   # served only by GET .../reel
        if entry.get("status") == "done" and "results" not in entry:
            # full results (with skeletons) live only on the volume — too big for the Dict
            vol.reload()
            rp = _job_dir(job_id) / "results.json"
            if rp.exists():
                entry["results"] = json.loads(rp.read_text(encoding="utf-8"))
        return entry

    # ---- coach reel -------------------------------------------------------
    @web.post("/api/jobs/{job_id}/reel", status_code=202)
    def create_reel(job_id: str, x_invite_code: str | None = Header(default=None)):
        check_invite(x_invite_code)
        entry = jobs.get(job_id)
        if entry is None:
            raise HTTPException(404, detail={"code": "not_found", "message": "No such job."})
        if entry.get("status") != "done":
            raise HTTPException(409, detail={"code": "not_ready",
                                             "message": "The analysis must finish before a reel can be rendered."})
        rs = entry.get("reel_status")
        if rs in ("queued", "running", "done"):   # idempotent
            return {"reel_status": rs}
        _set_status(job_id, reel_status="queued", reel_error=None)
        render_reel.spawn(job_id)
        return {"reel_status": "queued"}

    @web.get("/api/jobs/{job_id}/reel")
    def get_reel(job_id: str, x_invite_code: str | None = Header(default=None)):
        check_invite(x_invite_code)
        entry = jobs.get(job_id)
        if entry is None:
            raise HTTPException(404, detail={"code": "not_found", "message": "No such job."})
        out = {"reel_status": entry.get("reel_status"),
               "reel_percent": entry.get("reel_percent")}
        if entry.get("reel_error"):
            out["error"] = entry["reel_error"]
        if entry.get("reel_status") == "done":
            out["token"] = entry.get("reel_token")
            out["meta"] = entry.get("reel_meta")
        return out

    @web.get("/api/jobs/{job_id}/reel.mp4")
    def get_reel_file(job_id: str, t: str = Query(...), dl: int = Query(default=0)):
        # token auth, not the invite header: <video> tags and download links can't
        # send headers, and the invite code must never appear in a URL.
        from fastapi.responses import FileResponse
        entry = jobs.get(job_id)
        token = (entry or {}).get("reel_token")
        if not token or not hmac.compare_digest(t, token):
            raise HTTPException(403, detail={"code": "bad_token", "message": "Invalid reel link."})
        vol.reload()
        path = _job_dir(job_id) / "reel.mp4"
        if not path.exists():
            raise HTTPException(404, detail={"code": "not_found", "message": "Reel file missing."})
        # Chrome refuses inline <video> playback of attachment-disposition media,
        # so the player uses inline and only the download link passes dl=1.
        disposition = "attachment" if dl else "inline"
        return FileResponse(path, media_type="video/mp4", headers={
            "Content-Disposition": f'{disposition}; filename="baseline-coach-reel.mp4"'})

    return web


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------
@app.function(image=pipeline_image, volumes={str(VOL_PATH): vol},
              cpu=8.0, memory=8192, timeout=3600)
def analyze(job_id: str, params: dict):
    import sys
    sys.path.insert(0, "/root")
    import numpy as np
    from pipeline import ballgate, config, extract_pose, grading, segment

    jobdir = _job_dir(job_id)
    warnings: list[dict] = []

    def warn(code, message):
        warnings.append({"code": code, "message": message})

    def fail(code, message):
        _set_status(job_id, status="error", stage="error", percent=100,
                    error={"code": code, "message": message})

    def stage(name, percent, message=None):
        _set_status(job_id, status="running", stage=name, percent=int(percent),
                    **({"message": message} if message else {}))

    try:
        vol.reload()
        upload = jobdir / "upload.bin"
        norm_mp4 = jobdir / "normalized.mp4"

        # ---- 1. normalize -------------------------------------------------
        stage("normalizing", 2)
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", str(upload),
             "-vf", "scale='min(iw,1920)':'min(ih,1920)':force_original_aspect_ratio=decrease:force_divisible_by=2",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-an", str(norm_mp4)],
            capture_output=True, text=True, timeout=900)
        if r.returncode != 0 or not norm_mp4.exists():
            return fail("bad_video", "Could not decode that video - try exporting it as MP4 (H.264).")
        upload.unlink(missing_ok=True)
        vol.commit()

        # ---- 2. pose extraction -------------------------------------------
        config.DATA_DIR = jobdir
        config.DOMINANT_HAND = params["handedness"]
        stage("extracting_pose", 10)
        last = [0.0]

        def progress(idx, total):
            if total and time.time() - last[0] > 2:
                last[0] = time.time()
                stage("extracting_pose", 10 + 52 * idx / total)

        data = extract_pose.extract(norm_mp4, progress_cb=progress)
        n = data["n_frames"]
        pose_frames = int(np.sum(~np.isnan(data["world"][:, 0, 0]))) if n else 0
        pose_pct = pose_frames / n if n else 0
        if pose_pct < 0.20:
            return fail("no_pose", "Couldn't find a player in the video - film closer, "
                                   "with your full body in frame.")
        if pose_pct < 0.50:
            warn("low_pose_pct", f"Player visible in only {pose_pct:.0%} of frames - "
                                 "grades may be rough. Film closer next time.")

        # ---- 3. stroke detection ------------------------------------------
        stage("detecting_strokes", 64)
        strokes = segment.detect_strokes(norm_mp4, data=data)
        if not strokes:
            return fail("no_strokes", "No swings detected - make sure the clip shows "
                                      "you actually hitting balls.")

        # ---- 3b. vision ball-gate: keep only swings that actually hit a ball
        stage("verifying_hits", 66)
        strokes, gate = ballgate.verify_hits(norm_mp4, strokes, data)
        candidates = len(strokes)
        if gate["active"]:
            strokes = [s for s in strokes if s["contact_from_ball"]]
            if not strokes:
                return fail("no_ball_hits",
                            "Swings detected but none verifiably hit a ball - film closer "
                            "or more side-on so the ball is visible.")
        else:
            warn("ball_untracked",
                 "Couldn't track the ball in this clip - swings are NOT verified as real "
                 "hits. Film closer or at higher resolution for verified analysis.")

        # ---- 4. wing classify + grade every matching swing ------------------
        stage("filtering_wing", 70)
        W, H, fps = data["width"], data["height"], data["fps"]
        wing = params["stroke"]
        selected, stats = grading.grade_all(
            strokes, data["norm"], data["world"], W, H, fps, wing)
        if not selected:
            other = "backhand" if wing == "forehand" else "forehand"
            return fail("no_wing_strokes",
                        f"Found {stats['counts'][other]} {other}s but no clean {wing}s - "
                        f"try analyzing the {other} instead.")
        if len(selected) <= 2:
            warn("few_strokes", f"Only {len(selected)} clean {wing}s found - "
                                "a longer hitting clip gives a fairer report.")
        if stats["matching"] > stats["considered"]:
            warn("swings_sampled", f"{stats['matching']} {wing}s found; graded an evenly "
                                   f"time-spaced {stats['considered']} of them.")

        # ---- 5. heavy re-grade ---------------------------------------------
        stage("grading", 72)
        benchmark = params["benchmark"]
        ref_knee = grading.set_reference(benchmark, BENCHMARKS[benchmark],
                                         stroke=params["stroke"])
        if not grading.REF_MATCHED:
            warn("no_wing_benchmark",
                 f"No measured {params['stroke']} benchmark for "
                 f"{BENCHMARKS[benchmark]} yet - leg load is graded against a "
                 f"general guideline instead of their numbers.")
        contacts = [s["contact_frame"] for s, _ in selected]
        win = int(grading.WIN_SEC * fps) + 2
        hf, nn, ww = None, data["norm"], data["world"]
        hf = {f: f for f in range(nn.shape[0])}
        try:
            hz = extract_pose.extract_windows(
                norm_mp4, [(max(0, c - win), min(n - 1, c + win)) for c in contacts],
                seed_norm=data["norm"], variant="heavy", cache_tag="heavy")
            hf = {int(f): i for i, f in enumerate(hz["frames"])}
            nn, ww = hz["norm"], hz["world"]
        except Exception as e:  # noqa: BLE001
            warn("heavy_pass_failed", "High-fidelity re-grade failed; using the session-level pass.")
            print("[analyze] heavy pass failed:", e)

        stage("grading", 92)
        results, swings, dropped = [], [], 0
        speeds, fronts = [], []
        by_frame = {s["contact_frame"]: s for s, _ in selected}
        for c in contacts:
            try:
                snap = grading.SNAP_SEC_TIGHT if by_frame[c].get("contact_from_ball") else None
                a = grading.analyse(c, hf, nn, ww, W, H, fps, snap_sec=snap)
            except Exception:  # noqa: BLE001
                dropped += 1
                continue
            results.append(a)
            s = by_frame[c]
            path = grading.hand_path_points(a, hf, nn, W, H)
            speed = grading.swing_speed_kmh(a, hf, ww, fps)
            front = grading.contact_front_cm(a, hf, ww)
            speeds.append(speed)
            fronts.append(front)
            fverd = grading.front_verdict(front)
            swings.append({
                "idx": len(results),
                "contact_frame": int(a["contact"]),
                "time_s": round(a["contact"] / fps, 2),
                "contact_from_ball": bool(s.get("contact_from_ball")),
                "ball_confidence": s.get("ball_confidence", 0.0),
                "verdicts": {
                    "racket_drop":    {"label": a["drp"][0], "grade": a["drp"][1], "value": None},
                    "low_to_high":    {"label": a["lth"][0], "grade": a["lth"][1], "value": None},
                    "finish":         {"label": a["fin"][0], "grade": a["fin"][1], "value": None},
                    "contact_height": {"label": a["cverd"][0], "grade": a["cverd"][1],
                                       "value": round(float(a["chgt"]), 2)},
                    "leg_load":       {"label": a["lverd"][0], "grade": a["lverd"][1],
                                       "knee_deg": None if np.isnan(a["knee"]) else round(float(a["knee"]), 1)},
                },
                "overall": {"label": a["overall"][0], "grade": a["overall"][1],
                            "score": round(grading.score_of(a), 2), "note": a["note"]},
                "hand_speed_kmh": speed,
                "contact_front_cm": front,
                "contact_front": ({"label": fverd[0], "grade": fverd[1]} if fverd else None),
                "hand_path": path,
                "skeleton": grading.skeleton_frames(a, hf, nn),
            })
        if dropped:
            warn("swings_dropped", f"{dropped} swing(s) could not be graded and were skipped.")
        if not results:
            return fail("internal", "None of the detected swings could be graded.")

        # ---- 6. aggregate ---------------------------------------------------
        stage("aggregating", 96)
        agg = grading.aggregate(results, grading.REF_NAME, ref_knee,
                                speeds=speeds, fronts=fronts)
        payload = {
            "schema_version": 3,
            "job_id": job_id,
            "created_at": _utc_now(),
            "params": params,
            "video": {"duration_s": round(n / fps, 1), "fps": round(fps, 2),
                      "width": W, "height": H, "n_frames": n},
            "benchmark": {"name": benchmark, "display": BENCHMARKS[benchmark],
                          "knee_flexion_deg": round(ref_knee, 1),
                          # measured on this wing, or a generic threshold?
                          "wing": grading.REF_WING,
                          "wing_matched": grading.REF_MATCHED},
            "quality": {
                "pose_detect_pct": round(100 * pose_pct, 1),
                "strokes_detected_total": candidates,
                "ball_gate_active": gate["active"],
                "strokes_ball_verified": gate["hits"],
                "strokes_no_ball": candidates - gate["hits"],
                "strokes_matching_wing": stats["matching"],
                "strokes_considered": stats["considered"],
                "strokes_other_wing": stats["counts"]["backhand" if wing == "forehand" else "forehand"],
                "strokes_ambiguous": stats["counts"]["ambiguous"],
                "strokes_graded": len(results),
                "swings_dropped": stats["dropped"] + dropped,
                "warnings": warnings,
            },
            "skeleton_connections": grading.SKELETON_CONNECTIONS,
            "skeleton_wrist_i": grading.skeleton_wrist_index(params["handedness"]),
            "swings": swings,
            "aggregate": agg,
        }
        # numpy scalars (float32 etc.) leak from the grading math; sanitize once.
        def np_default(o):
            if hasattr(o, "item"):
                return o.item()
            raise TypeError(f"not JSON serializable: {type(o)}")
        payload = json.loads(json.dumps(payload, default=np_default))
        # With per-swing skeletons the payload is far too big for the Dict —
        # it lives ONLY on the volume; GET /api/jobs/{id} reads it from there.
        (jobdir / "results.json").write_text(json.dumps(payload), encoding="utf-8")
        vol.commit()
        _set_status(job_id, status="done", stage="done", percent=100, results_ready=True)
        print(f"[analyze] {job_id} done: {len(results)} swings graded")
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        fail("internal", "Analysis failed unexpectedly. Try a shorter or clearer clip.")
        raise


# ---------------------------------------------------------------------------
# Coach reel worker
# ---------------------------------------------------------------------------
@app.function(image=pipeline_image, volumes={str(VOL_PATH): vol},
              cpu=8.0, memory=8192, timeout=1800)
def render_reel(job_id: str):
    import sys
    sys.path.insert(0, "/root")
    from pipeline import config, extract_pose
    from pipeline import reel as reel_mod

    jobdir = _job_dir(job_id)
    try:
        vol.reload()
        results = json.loads((jobdir / "results.json").read_text(encoding="utf-8"))
        params = results["params"]
        _set_status(job_id, reel_status="running", reel_percent=2)

        config.DATA_DIR = jobdir
        config.DOMINANT_HAND = params["handedness"]
        norm_mp4 = jobdir / "normalized.mp4"
        data = extract_pose.load(norm_mp4)      # session npz cache from the analyze job

        contacts = [s["contact_frame"] for s in results["swings"]]
        best_idx = results["aggregate"].get("best_swing_idx")   # 1-based
        picked = reel_mod.pick_reel_contacts(
            contacts, None if best_idx is None else best_idx - 1)

        last = [0.0]

        def progress(i, total):
            if time.time() - last[0] > 3:
                last[0] = time.time()
                _set_status(job_id, reel_status="running",
                            reel_percent=int(2 + 95 * i / max(1, total)))

        bench = results["benchmark"]
        # Unmatched wing => the number is a general guideline, so don't put the
        # pro's name next to it on the reel either.
        ref_name = (bench["display"] if bench.get("wing_matched", True)
                    else "loaded")
        meta = reel_mod.render(norm_mp4, data, picked, jobdir / "reel.mp4",
                               stroke=params["stroke"], ref_name=ref_name,
                               ref_knee=bench["knee_flexion_deg"], cache_tag="heavy",
                               progress_cb=progress)
        vol.commit()
        _set_status(job_id, reel_status="done", reel_percent=100,
                    reel_token=uuid.uuid4().hex, reel_meta=meta)
        print(f"[reel] {job_id} done: {meta}")
    except Exception:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        _set_status(job_id, reel_status="error",
                    reel_error={"code": "reel_failed",
                                "message": "Reel render failed - tap Generate to retry."})
        raise
