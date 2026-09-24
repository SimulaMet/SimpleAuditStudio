---
title: SimpleAudit Studio
emoji: 🔍
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

# SimpleAudit Studio
<a href="https://sushantgautam-simpleaudit-studio.hf.space" target="_blank" rel="noopener noreferrer">
  <img alt="SimpleAudit Studio — a platform for reproducible AI model audits" src="https://github.com/user-attachments/assets/d9e0105a-9e2c-4455-a855-ad5854fd604f" />
</a>

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Python](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](https://www.python.org/)
[![PyPI](https://img.shields.io/pypi/v/simpleaudit-studio)](https://pypi.org/project/simpleaudit-studio/)

A self-hostable platform for running reproducible AI model audits using the [SimpleAudit](https://github.com/kelkalot/simpleaudit) engine (Target → Auditor → Judge). Every audit captures frozen, versioned inputs so historical results stay interpretable years later.

## 🚀 Quick Start

```bash
uvx simpleaudit-studio
```

[`uvx`](https://docs.astral.sh/uv/#uvx) installs the [`simpleaudit-studio`](https://pypi.org/project/simpleaudit-studio/) package and runs it in an isolated environment. No Docker, no Postgres, no manual setup. Opens at http://localhost:8000 (login: `studio` / `admin123`). A mock model server is pre-seeded so you can start exploring audit results immediately.

🌐 Or skip the setup entirely — try the live demo: <a href="https://sushantgautam-simpleaudit-studio.hf.space" target="_blank" rel="noopener noreferrer">sushantgautam-simpleaudit-studio.hf.space</a>

### 🤖 Using Real Models

The pre-seeded connections point at a built-in mock server. To run real audits, update your model connections on the **Models** page to any OpenAI-compatible endpoint. A few common options:

| Provider | Base URL | Example |
|----------|----------|---------|
| Ollama (local) | `http://localhost:11434/v1` | Qwen 3, Llama 3.3, Mistral |
| vLLM (GPU) | `http://your-gpu-server:8000/v1` | Any HF model |
| OpenAI | `https://api.openai.com/v1` | GPT-4o, o3 |
| Together AI | `https://api.together.xyz/v1` | Many open models |
| Groq | `https://api.groq.com/openai/v1` | Fast inference |

Set the API key in the connection form (leave blank for local servers that don't require auth), then launch an audit from the Scenario Library.

## 🐳 Self-Hosting

For teams, multi-user setups, or GPU worker pools, use Docker Compose:

```bash
git clone https://github.com/SushantGautam/SimpleAuditStudio
cd SimpleAuditStudio
cp .env.example .env
# edit POSTGRES_PASSWORD and BOOTSTRAP_PASSWORD at minimum
docker compose up -d
```

Services: Web UI (:8000), PostgreSQL, Hatchet queue (:8888), Worker. Optional profiles: `--profile storage` (MinIO), `--profile mock` (mock model API).

See [docs/deployment.md](docs/deployment.md) for production hardening, GPU workers, backups, and upgrades.

## ✨ What You Can Do

- Browse the scenario library and create new audit scenarios
- Register model endpoints (OpenAI-compatible APIs)
- Launch audits and watch live progress
- Compare results across runs
- Explore the visualizer for detailed analysis

## 🏗️ Architecture

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

## 📚 Documentation

- [Architecture](docs/architecture.md) — system design, service boundaries, deployment topology
- [Domain Model](docs/domain-model.md) — scenarios, scenario sets, audit runs, immutability invariants
- [Deployment](docs/deployment.md) — Docker Compose, production hardening, GPU workers, upgrades

## 🤝 Contributing

Architecture decisions are documented in [docs/architecture.md](docs/architecture.md).








