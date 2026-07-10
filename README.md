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
  │               stroke detection -> wing classify -> grade EVERY matching
  │               swing (evenly time-spaced cap 30) -> heavy re-grade ->
  │               results JSON (schema 2, with per-swing skeletons)
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
- No ball tracking: shot placement/depth/spin (SwingVision-style) are out of
  reach for a pose-only pipeline and deliberately not faked.
- Hip–shoulder separation (X-factor) deferred until per-pro reference values
  are measured — next reference-clip task.
