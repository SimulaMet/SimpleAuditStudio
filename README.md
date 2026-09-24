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

## Quick Start (Minimal Config)

Run SimpleAudit Studio locally in seconds — no Docker, no Postgres, no infrastructure:

```bash
uvx simpleaudit-studio
```

This boots a full local stack in one process:
- **Django web UI** at http://localhost:8000 (login: `admin` / `admin12345`)
- **Hatchet workflow engine** (embedded mode — real persistent queue + worker, no Docker)
- **SQLite database** (auto-created, persists across restarts)
- **Mock model server** (pre-seeded so you can try an audit immediately)

First run downloads a ~53 MB Hatchet sidecar binary (cached in `~/.hatchet/`).
Subsequent starts are faster. Press `Ctrl+C` to stop.

### Using Real Models

The pre-seeded model connections point at a built-in mock server so you can
explore the UI without any setup. To run **real audits**, update your model
connections in the UI (Models page) to point at any OpenAI-compatible endpoint:

| Provider | Base URL | Example |
|----------|----------|---------|
| Ollama (local) | `http://localhost:11434/v1` | Qwen 3, Llama 3.3, Mistral |
| vLLM (GPU) | `http://your-gpu-server:8000/v1` | Any HF model |
| OpenAI | `https://api.openai.com/v1` | GPT-4o, o3 |
| Together AI | `https://api.together.xyz/v1` | Many open models |
| Groq | `https://api.groq.com/openai/v1` | Fast inference |

Set the API key in the connection form (or leave blank for local servers that
don't require auth). Then launch an audit from the Scenario Library.

### When to Use What

| Setup | Best for |
|-------|----------|
| `uvx simpleaudit-studio` | Small projects, research, single user, quick experiments, local GPU audits |
| [Docker Compose](#self-hosting) | Teams, multi-user, production, GPU worker pools, long-running jobs |

The minimal config uses the **same code paths** as the full deployment — same
worker, same engine, same queue semantics. The only differences are SQLite
instead of Postgres and embedded Hatchet instead of a standalone server.

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
docker build \
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
