---
title: SimpleAudit Studio
emoji: 🔍
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

# SimpleAudit Studio — AI Model Audit Platform

A production-quality platform for running reproducible AI model audits using the
[SimpleAudit](https://github.com/kelkalot/simpleaudit) engine (Target → Auditor → Judge).

## Demo Credentials

- **Username:** `admin`
- **Password:** `admin123`

## What You Can Do

- Browse the scenario library and create new audit scenarios
- Register model endpoints (OpenAI-compatible APIs)
- Launch audits and watch live progress
- Compare results across runs
- Explore the visualizer for detailed analysis

## Architecture

This Space runs the full stack in a single container:

```
Browser → Django/Gunicorn (:7860)
              ↓
         PostgreSQL 16 (domain + queue DBs)
              ↓
         Hatchet Server (durable job queue)
              ↓
         Worker (CPU pool) → SimpleAudit Engine
              Target → Auditor → Judge
```

## Notes

- **Ephemeral storage**: All data is lost when the Space restarts or is redeployed.
  This is a demo, not a persistent deployment.
- **CPU-only**: Audits run on CPU. For GPU-backed model inference, point your
  model endpoint at an external API.
- **Cold start**: The first request after idle may take 30–60 seconds while all
  services initialize.

---

# Deployment Guide

Two guides below. Pick the one that matches your experience level.

- **[Beginner Guide](#beginner-guide-step-by-step)** — explains what each step does, why it's needed, and how to verify it worked.
- **[Expert Quick Reference](#expert-quick-reference)** — just the commands.

---

## Beginner Guide (Step by Step)

This guide walks you through deploying SimpleAudit Studio to a **Hugging Face Space** (free hosting) or to **your own machine** (Docker Compose). Read top to bottom.

### What is being deployed?

SimpleAudit Studio is a full web application. It needs four things running at once:

| Component | What it does |
|-----------|-------------|
| **Web server** (Django/Gunicorn) | The UI you see in your browser. Listens on port 7860. |
| **PostgreSQL** | Stores users, scenarios, audit runs, and results. |
| **Hatchet Server** | A job queue — tracks which audits are queued, running, done, or failed. |
| **Worker** | Actually runs the audits (calls the Target → Auditor → Judge pipeline). |

On Hugging Face Spaces, all four run inside **one Docker container**. On your own machine, Docker Compose runs them as separate containers (easier to manage).

### Option A: Deploy to a Hugging Face Space (free, no server needed)

#### Prerequisites

1. A [Hugging Face account](https://huggingface.co/join) (free).
2. A [git access token](https://huggingface.co/settings/tokens) — create one with **Write** access. You'll use this to push code to the Space.
3. Git installed on your machine (`git --version` should work).

#### Step 1: Create the Space

Go to [huggingface.co/new-space](https://huggingface.co/new-space) and:

- **Space name:** `simpleaudit-studio` (or whatever you like)
- **SDK:** select **Docker** (important — not Gradio or Streamlit)
- **Hardware:** CPU Basic (free tier works; audits run on CPU)

Click **Create**. HF will initialize an empty repo for you.

> **Why Docker SDK?** Our app isn't a Gradio/Streamlit demo — it's a multi-process Django app with Postgres and a job queue. Only the Docker SDK lets us run arbitrary processes.

#### Step 2: Push the code

The Space has its own git repository (separate from GitHub). You push directly to it:

```bash
# Clone the Space's repo (replace <your-hf-username>)
git clone https://huggingface.co/spaces/<your-hf-username>/simpleaudit-studio
cd simpleaudit-studio

# Copy all project files into it (from your local checkout of the GitHub repo)
cp -r /path/to/SimpleAuditStudio/* .
cp /path/to/SimpleAuditStudio/.dockerignore .
cp /path/to/SimpleAuditStudio/.env.example .

# Commit and push
git add .
git commit -m "Deploy SimpleAudit Studio"
git push origin main
```

> **What happens when you push?** HF detects the new commit, builds the Docker image (takes ~5–10 min the first time), then starts the container. You can watch progress at:
> `https://huggingface.co/spaces/<your-hf-username>/simpleaudit-studio`
> The page shows **Building…** → **Running** when ready.

> **Troubleshooting: "push rejected"**
> If HF rejects your push because a file is too large (>~500 KB), find the offending file and either remove it or resize it. Common culprits: large images, model weights, or log files. Add them to `.dockerignore` and `.gitignore`.

#### Step 3: Wait for the build

Open the Space URL in your browser. You'll see:

1. **"Building…"** — HF is running `docker build`. First build takes 5–10 minutes. Subsequent builds are faster (layer caching).
2. **"Running"** — the container started. Click the app link.
3. **First request may take 30–60 seconds** — all services (Postgres, Hatchet, Worker, Web) initialize on first hit. This is normal.

If you see **"App process crashed"**, check the logs:

```bash
# Get runtime logs (SSE stream)
curl -N -H "Authorization: Bearer $HF_TOKEN" \
  "https://huggingface.co/api/spaces/<your-hf-username>/simpleaudit-studio/logs/run"

# Get build logs
curl -N -H "Authorization: Bearer $HF_TOKEN" \
  "https://huggingface.co/api/spaces/<your-hf-username>/simpleaudit-studio/logs/build"
```

Common crash causes:
- Missing environment variables (see Step 4)
- Port mismatch (must be 7860 — set in `README.md` frontmatter)
- Out-of-memory on free tier (unlikely for our app)

#### Step 4: Set environment variables (optional but recommended)

By default, the app uses safe defaults (admin/admin123, random secret key). For anything beyond a demo, set these in **Space Settings → Variables & Secrets**:

| Variable | Purpose | Default |
|----------|---------|---------|
| `DJANGO_SECRET_KEY` | Cryptographic signing key. **Must change in production.** | Random per-build |
| `BOOTSTRAP_ADMIN_PASSWORD` | Initial admin password | `admin123` |
| `DJANGO_ALLOWED_HOSTS` | Allowed hostnames | Auto-detected from HF domain |

To set them: go to your Space → **Settings** tab → **Variables & Secrets** → add each variable. Then click **Restart** to apply.

> **Why not put these in `.env`?** HF Spaces don't read `.env` files. Environment variables must be set via the Settings UI (they're injected into the container at runtime).

#### Step 5: Verify it works

1. Open `https://<your-hf-username>-simpleaudit-studio.hf.space/login/`
2. Log in with `admin` / `admin123` (or your custom password)
3. You should see the Dashboard
4. Try registering a new user at `/register/`
5. Check `/healthz` returns `{"status":"ok"}`

✅ If all of the above work, your deployment is successful.

---

### Option B: Self-host with Docker Compose (persistent, full control)

This is the **recommended** option for real use. Data persists across restarts, you control the hardware, and you can attach GPU workers.

#### Prerequisites

1. [Docker](https://docs.docker.com/get-docker/) + [Docker Compose](https://docs.docker.com/compose/install/) installed
2. At least 4 GB RAM (Postgres + Hatchet + Worker + Web)
3. A machine you can keep running (VPS, home server, etc.)

#### Step 1: Get the code

```bash
git clone https://github.com/SushantGautam/SimpleAuditStudio
cd SimpleAuditStudio
```

#### Step 2: Configure

```bash
cp .env.example .env
```

Edit `.env` and **at minimum** change:

```env
DJANGO_SECRET_KEY=generate-a-long-random-string   # e.g. openssl rand -hex 32
BOOTSTRAP_ADMIN_PASSWORD=your-strong-password
POSTGRES_PASSWORD=your-db-password
```

> **Why these three?** The secret key signs session cookies and CSRF tokens — if leaked, attackers can forge sessions. The admin password is your login. The DB password protects your data.

#### Step 3: Start the stack

```bash
docker compose up -d
```

This builds the image (first time: ~5 min) and starts four services:

```
postgres       → database (port 5432, internal only)
hatchet-server → job queue (port 8250, internal only)
worker         → runs audits (no exposed port)
web            → Django UI (port 7860 → localhost:7860)
```

Watch the startup:

```bash
docker compose logs -f
```

You'll see migrations run, the admin user bootstrap, and Gunicorn start. When you see `Listening at: http://0.0.0.0:7860`, it's ready.

#### Step 4: Verify

1. Open `http://localhost:7860/login/` in your browser
2. Log in with `admin` / your password
3. Check `http://localhost:7860/healthz` → `{"status":"ok"}`

✅ Done. Your persistent deployment is live.

#### Day-to-day operations

```bash
# Stop everything
docker compose down

# Start again (data persists in the postgres volume)
docker compose up -d

# View logs
docker compose logs -f web        # web server logs
docker compose logs -f worker     # audit execution logs
docker compose logs -f hatchet    # job queue logs

# Update to latest code
git pull
docker compose up -d --build      # rebuild image + restart

# Backup the database
docker compose exec postgres pg_dump -U simpleaudit simpleaudit > backup.sql

# Restore
docker compose exec -T postgres psql -U simpleaudit simpleaudit < backup.sql
```

#### Optional: enable object storage (for large audit artifacts)

```bash
docker compose --profile storage up -d
```

This adds MinIO (S3-compatible storage) for storing raw audit outputs. Configure `MINIO_*` vars in `.env`.

---

### Troubleshooting (both options)

| Symptom | Cause | Fix |
|---------|-------|-----|
| Page loads but shows "App process crashed" | Container exited | Check logs (see above). Usually a missing env var or port issue. |
| Login fails with 403 "Origin checking failed" | CSRF origins not configured | Our code auto-derives `CSRF_TRUSTED_ORIGINS` from `ALLOWED_HOSTS`. Ensure `DJANGO_ALLOWED_HOSTS` includes your domain. |
| Register page shows 500 | Old code (pre-fix) | Make sure you pushed the latest commit. The fix changed `RegisterView` to use `TemplateView`. |
| Logo/icon not showing | File too large for HF | Keep `static/logo.png` under 500 KB. Resize: `sips -z 128 128 logo.png` (macOS) or `convert logo.png -resize 128x128 logo.png` (ImageMagick). |
| Build takes forever every time | Cache-busting artifacts in Dockerfile | Don't add `ARG BUILD_ID` or cache-bust comments. They invalidate all layers. |
| Data disappears after restart | Ephemeral storage (HF) or missing volume (Compose) | On HF: expected (use Storage Bucket for persistence). On Compose: ensure the `pgdata` volume exists (`docker volume ls`). |
| First request hangs 30–60s | Cold start | Normal. All services initialize on first hit. Subsequent requests are fast. |

---

## Expert Quick Reference

### HF Space deploy

```bash
# 1. Create Space (Docker SDK, CPU) at huggingface.co/new-space
# 2. Push code
git clone https://huggingface.co/spaces/$USER/simpleaudit-studio && cd $_
cp -r /path/to/SimpleAuditStudio/{*,.[!.]*} .
git add . && git commit -m "deploy" && git push origin main
# 3. Watch: huggingface.co/spaces/$USER/simpleaudit-studio
# 4. Set vars in Settings → Variables & Secrets (DJANGO_SECRET_KEY, BOOTSTRAP_ADMIN_PASSWORD)
# 5. Verify: curl -s https://${USER}-simpleaudit-studio.hf.space/healthz
```

### Docker Compose deploy

```bash
git clone https://github.com/SushantGautam/SimpleAuditStudio && cd $_
cp .env.example .env
# edit .env: DJANGO_SECRET_KEY, BOOTSTRAP_ADMIN_PASSWORD, POSTGRES_PASSWORD
docker compose up -d
# verify
curl -s http://localhost:7860/healthz
```

### Useful API endpoints (HF)

```bash
# Status
curl -s -H "Accept: application/json" \
  "https://huggingface.co/api/spaces/$USER/simpleaudit-studio"

# Runtime logs (SSE)
curl -N -H "Authorization: Bearer $HF_TOKEN" \
  "https://huggingface.co/api/spaces/$USER/simpleaudit-studio/logs/run"

# Build logs (SSE)
curl -N -H "Authorization: Bearer $HF_TOKEN" \
  "https://huggingface.co/api/spaces/$USER/simpleaudit-studio/logs/build"

# Restart (app only, NOT image rebuild)
curl -X POST -H "Authorization: Bearer $HF_TOKEN" \
  "https://huggingface.co/api/spaces/$USER/simpleaudit-studio/restart"
```

### Key facts

- **Port:** 7860 (set in root `README.md` frontmatter: `app_port: 7860`)
- **Dockerfile:** root `Dockerfile` (single-container, supervisord-managed)
- **Data:** ephemeral on HF (lost on restart); persistent on Compose (named volume)
- **CPU-only** on free tier; point model endpoints at external APIs for GPU inference
- **Cold start:** 30–60s first request after idle
- **Demo creds:** `admin` / `admin123` (change via `BOOTSTRAP_ADMIN_PASSWORD`)
