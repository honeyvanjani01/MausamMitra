# Deploying @MausamMitra (Render) — single-service build

This build is **one service**, not two: the FastAPI backend now also serves
the frontend directly (see the `StaticFiles` mount at the end of
`backend/main.py`), and `frontend/app.js` talks to the API on the **same
origin** (`DEFAULT_API_BASE = ""`). So there's nothing to "wire together" —
whatever URL the one service gets, opening it in a browser just works.

You already have:
- GitHub repo: `https://github.com/honeyvanjani01/MausamMitra`
- A Render service from an earlier (two-service) build at
  `https://mausammitra-1ei0.onrender.com`

The steps below replace that old two-service setup with this new
single-service one, using the same repo.

## 1. Push this folder to your existing GitHub repo

On your Windows machine, in the folder where you unzipped this
(`MausamMitra_PS26076` or similar), open PowerShell there and run:

```powershell
cd C:\path\to\the\unzipped\folder
git init
git remote add origin https://github.com/honeyvanjani01/MausamMitra.git
git add .
git commit -m "Single-service build with persona homepage + i18n"
git branch -M main
git push -u origin main --force
```

- `--force` is needed because this replaces the old two-service code
  structure with the new one — it overwrites whatever is on GitHub now.
- If `git` asks you to sign in, use a GitHub **personal access token** as
  the password (GitHub → Settings → Developer settings → Personal access
  tokens), not your account password.
- If the repo already has commits you want to keep, skip `--force` and
  instead run `git pull --allow-unrelated-histories` first, resolve any
  conflicts, then push normally.

## 2. Re-point Render at the new single-service setup

Your old Render service was built for the two-service layout, so the
cleanest path is a fresh **Blueprint** deploy (Render reads `render.yaml`
and creates the one `mausammitra` web service automatically):

1. Render dashboard → **New +** → **Blueprint** → pick the
   `honeyvanjani01/MausamMitra` repo (branch `main`).
2. Render finds `render.yaml` and shows one service: `mausammitra`.
3. When prompted, paste your **Gemini API key** as `GEMINI_API_KEY`
   (get/rotate it at https://aistudio.google.com/apikey — never hardcode
   it in a file you commit).
4. Click **Apply** / **Create**. First deploy takes 2–5 minutes.
5. Once live, open that service's `.onrender.com` URL — that's the one
   link to share. It serves both the UI and the API.
6. Delete the old two-service setup (the previous backend + frontend
   services) from the dashboard once the new one is confirmed working, so
   you're not paying attention to (or confusing testers with) two live
   URLs.

   Alternatively, if you'd rather keep the same old URL
   (`mausammitra-1ei0.onrender.com`): open that existing **backend**
   service's Settings → change **Root Directory** to `backend` (already
   set) and confirm **Start Command** is
   `uvicorn main:app --host 0.0.0.0 --port $PORT` — then just push (step 1)
   and it auto-redeploys on the same URL. You can then delete the separate
   old frontend service, since the backend now serves the frontend too.

## 3. Before you post the link publicly

- [ ] Confirm `GEMINI_API_KEY` is set as a real env var on the service
      (Render dashboard → service → Environment), not hardcoded anywhere.
- [ ] Confirm `DEMO_MODE` is `false` (already set in `render.yaml`) so it
      answers live questions instead of canned demo replies.
- [ ] Open the deployed URL yourself, on both desktop and phone, before
      sharing — Render's free tier spins a service down after inactivity,
      so the very first request after a while can take ~30-50s to wake up.
      Worth a one-line heads-up when you share it ("give it a few seconds
      to wake up").
- [ ] Grant location permission when your browser asks, so the
      personalized-homepage cards (AQI, UV, running hours, sea conditions,
      crop advisory, etc.) have a location to work from.

## If you don't want to use git/GitHub at all

Render doesn't support drag-and-drop zip deploys for services with a build
step, so a Python backend always needs a Git push. The only no-GitHub
options are services that accept a CLI push straight from a local folder
(Railway: `railway up`, Fly.io: `fly deploy`) — ask if you'd like those
steps instead.
