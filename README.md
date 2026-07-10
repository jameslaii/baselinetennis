# Baseline — Tennis Stroke Lab

Upload a 1–2 minute tennis clip, get your forehand or two-handed backhand graded
against a measured pro benchmark (Carlos Alcaraz, Alex Eala).

**Live site:** https://baselinetennis.vercel.app (invite code required)

## Architecture

```
index.html + app.js          static frontend on Vercel (no framework, no build step)
        |
        v  POST /api/jobs  (raw video stream + X-Invite-Code header)
backend/modal_app.py         Modal app "baseline" (workspace jameslaii)
  ├─ api        FastAPI: upload validation, job status
  └─ analyze    worker (8 CPU): ffmpeg normalize -> MediaPipe pose ->
                stroke detection -> wing filter -> top-10 grading -> results JSON
backend/pipeline/            analysis code vendored from the tennis-coach desktop project
```

- Job state lives in a Modal Dict (`baseline-jobs`), files on a Modal Volume (`baseline-videos`).
- The invite code is a Modal Secret (`baseline-invite`), checked server-side. Daily cap: 15 jobs.
- Uploads: ≤ 155 s, ≤ 500 MB (4K exports exceed this — the site tells users to export 1080p).

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

## v1 scope (honest limits)

- Forehands + two-handed backhands only (wing detection uses inter-wrist gap;
  one-handed backhands would misclassify as forehands).
- Benchmarks with real measured references: Alcaraz (knee 135°), Eala (134°).
  Adding a pro = one court-level clip through the desktop `make_reference.py`.
- Report card only; no rendered coach-reel video (yet — the normalized video and
  pose caches are kept on the volume for exactly that).
