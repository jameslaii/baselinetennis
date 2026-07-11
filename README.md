# Baseline — Tennis Stroke Lab

Upload a 1–4 minute tennis clip, get your forehand or two-handed backhand graded
against a measured pro benchmark (Carlos Alcaraz, Alex Eala) — plus an on-demand
slow-mo coach reel and an in-browser skeleton replay of every graded swing.

**Live site:** https://baselinetennis.vercel.app (invite code required)

## Architecture

```
index.html + app.js          static frontend on Vercel (no framework, no build step)
        |
        v  POST /api/jobs  (raw video stream + X-Invite-Code header)
backend/modal_app.py         Modal app "baseline" (workspace jameslaii)
  ├─ api          FastAPI: upload validation, job status, reel request/serve
  ├─ analyze      worker (8 CPU): ffmpeg normalize -> MediaPipe pose ->
  │               stroke candidates (wrist-speed peaks) -> VISION BALL-GATE
  │               (verify each candidate actually struck a ball; re-anchor
  │               contact on the ball's direction change) -> wing classify ->
  │               grade EVERY matching swing (evenly time-spaced cap 30) ->
  │               heavy re-grade -> results JSON (schema 3, per-swing skeletons)
  └─ render_reel  worker: slow-mo handpath reel (reuses the heavy pose cache,
                  ~1-3 min), served via a per-job random token
backend/pipeline/            analysis code vendored from the tennis-coach desktop project
```

- Job state lives in a Modal Dict (`baseline-jobs`); files AND full results on a
  Modal Volume (`baseline-videos`) — with skeletons the results JSON is too big
  for the Dict, so `GET /api/jobs/{id}` reads `results.json` from the volume.
- The invite code is a Modal Secret (`baseline-invite`), checked server-side. Daily cap: 15 jobs.
- Uploads: ≤ 250 s, ≤ 500 MB (4K exports exceed this — the site tells users to export 1080p).
- Reel downloads use `?t=<uuid>` token auth — `<video>` tags can't send headers,
  and the invite code must never appear in a URL.
- Grading is distribution-honest: v1 graded the top-10 swings *by score* which
  flattered the averages; v2 grades every matching swing (evenly time-spaced
  subset if >30) and reports typical-vs-best.
- Contact detection is pure vision (v4): the old audio "pock" snapping is gone —
  it was inaudible outdoors and never verified a hit. `pipeline/ballgate.py`
  re-reads a short window around each wrist-speed peak, tracks small fast-moving
  blobs (three-frame differencing, player core masked, thresholds scaled by
  player size and fps) and calls a swing a HIT only when a trajectory enters
  racket reach and changes direction there. The gate is adaptive: if almost no
  candidates verify, the ball probably isn't visible (far camera / blur), so all
  swings are kept with a loud `ball_untracked` warning instead of failing.

## v3 metrics (SwingVision-inspired, pose-honest)

- **Hand speed (km/h)** — peak dominant-wrist speed around contact from MediaPipe
  *world* landmarks (metric, hip-origin). Adjacent-pair smoothed, 5–60 km/h sanity
  band. Labelled honestly as *hand* speed (racket head runs ~1.5–2× faster).
- **Contact out front (cm)** — wrist offset from the hip centre at contact,
  projected on the horizontal swing direction. ≥15 cm good / 0–15 close / <0 behind.
- **Consistency + trend** — score std-dev rating (machine-like / steady / streaky)
  and first-third vs last-third fade line.
- **This Week plan** — top-2 measured issues with drills, plus a filming tip
  derived from the job's warnings.
- **Progress strip** — home screen sparkline of avg swing score across past
  sessions (localStorage), with hand-speed and knee deltas vs the previous session.

## Deploying

Frontend (from the repo root):

```
npx vercel deploy --prod --yes
```

Backend (Windows needs the UTF-8 flag):

```
PYTHONUTF8=1 python -m modal deploy backend/modal_app.py
```

## Local checks

`backend/smoke_test.py` runs the grading logic against cached pose data from the
desktop tennis-coach project (no mediapipe needed):

```
C:\Users\james\tennis-coach\.venv\Scripts\python.exe backend\smoke_test.py
```

## Gotchas learned the hard way

- mediapipe on debian_slim needs `libgles2` + `libegl1` (plus `libgl1`, `libglib2.0-0`).
- numpy float32 leaks into the results JSON — the worker sanitizes with `json.dumps(default=o.item())`.
- Vercel aliases sit behind SSO deployment protection; only the project's own
  production domain is public (hence the project is named `baselinetennis`).
- `.vercelignore` keeps `backend/` off the public site — do not remove it.

## Scope (honest limits)

- Forehands + two-handed backhands only (wing detection uses inter-wrist gap;
  one-handed backhands would misclassify as forehands).
- Benchmarks with real measured references: Alcaraz (knee 135°), Eala (134°).
  Adding a pro = one court-level clip through the desktop `make_reference.py`.
- Ball tracking is classical CV and used ONLY to verify hits and anchor contact
  frames — good enough for gating, not for shot placement/depth/spin
  (SwingVision-style), which stay deliberately unfaked. A learned tracker
  (TrackNet) is the upgrade path if the gate needs more reach.
- Hip–shoulder separation (X-factor) deferred until per-pro reference values
  are measured — next reference-clip task.
