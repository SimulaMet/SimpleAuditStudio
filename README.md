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

## Self-Hosting

### Quick start (single container, no clone needed)

Build and run directly from GitHub — no local checkout required:

```bash
docker build -f deploy/hf-space/Dockerfile \
  https://github.com/SushantGautam/SimpleAuditStudio.git#main \
  -t simpleaudit-studio

docker run -d -p 7860:7860 --name simpleaudit simpleaudit-studio
```

Open http://localhost:7860 — log in with `admin` / `admin123`.

> **Note:** This single-container mode uses ephemeral storage. Data is lost on
> restart. For persistent deployments with separate Postgres/Hatchet/worker
> containers, use Docker Compose below.

### Full stack (Docker Compose, persistent)

```bash
git clone https://github.com/SushantGautam/SimpleAuditStudio
cd SimpleAuditStudio
cp .env.example .env
# edit POSTGRES_PASSWORD and BOOTSTRAP_ADMIN_PASSWORD at minimum
docker compose up -d
```

Services: Web UI (:8000), PostgreSQL, Hatchet queue (:8888), Worker.
Optional profiles: `--profile storage` (MinIO), `--profile mock` (mock model API).

See [docs/deployment.md](docs/deployment.md) for production hardening, GPU
workers, backups, and upgrade procedures.
