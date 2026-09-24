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

## Deploy to Hugging Face Spaces

This repo is designed to be pushed **as-is** to an HF Space. The root
`README.md` contains the required YAML frontmatter (`sdk: docker`,
`app_port: 7860`) and the root `Dockerfile` is what HF builds.

1. Create a new Space: [huggingface.co/spaces/new](https://huggingface.co/spaces/new)
   - **SDK:** Docker
   - **Hardware:** `cpu-basic` minimum (GPU if you want local model inference)
2. In Space **Settings → Source**, link this GitHub repo:
   `SushantGautam/SimpleAuditStudio` (branch `main`)
   — or push directly: `git remote add space https://huggingface.co/spaces/<user>/<space>` then `git push space main`
3. Set these **Space Secrets** (Settings → Repository secrets):
   - `POSTGRES_PASSWORD` — any strong string
   - `DJANGO_SECRET_KEY` — any long random string
   - `HATCHET_ADMIN_TOKEN` — any random token
4. Click **Save**. First build takes ~5 min.

The Space will be live at `https://<your-username>.hf.space/<space-name>/`.

> **Note:** This `deploy/hf-space/` folder contains an alternative
> single-container Dockerfile (with supervisord) for reference. The live
> Space uses the **root** `Dockerfile` instead.

## Self-Hosting

For a persistent, production deployment, use Docker Compose:

```bash
git clone https://github.com/SushantGautam/SimpleAuditStudio
cd SimpleAuditStudio
cp .env.example .env
docker compose up -d
```

See the [full documentation](https://github.com/SushantGautam/SimpleAuditStudio#readme) for details.
