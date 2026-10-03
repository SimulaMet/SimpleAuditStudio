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

A self-hostable platform for running reproducible AI model audits using the [SimpleAudit](https://github.com/SimulaMet/simpleaudit) engine (Target → Auditor → Judge). Every audit captures frozen, versioned inputs so historical results stay interpretable years later.

## 🚀 Quick Start

Try it in one line (needs [uv](https://docs.astral.sh/uv/), a fast Python tool installer):

```bash
uvx simpleaudit-studio@latest
```

Opens at <http://localhost:8000> — log in as `studio` / `admin123`. Your data is kept in `~/.simpleaudit-studio/` and survives restarts and updates. There is no other setup — no Docker, no database server, no manual configuration.

🌐 Prefer to skip setup entirely? Try the live demo: <a href="https://sushantgautam-simpleaudit-studio.hf.space" target="_blank" rel="noopener noreferrer">sushantgautam-simpleaudit-studio.hf.space</a>

The full list of all four ways to run it is in [Four ways to run it](#-four-ways-to-run-it).


### 🤖 Using Real Models

The pre-seeded connections point at a built-in mock server. To run real audits, update your model connections on the **Models** page to any OpenAI-compatible endpoint. A few common options:

| Provider | Base URL | Example |
|----------|----------|---------|
| Ollama (local) | `http://localhost:11434/v1` | Qwen 3, Llama 3.3, Mistral |
| vLLM | `http://your-vllm-server:8000/v1` | Any HF model |
| OpenAI | `https://api.openai.com/v1` | GPT-4o, o3 |
| Together AI | `https://api.together.xyz/v1` | Many open models |
| Groq | `https://api.groq.com/openai/v1` | Fast inference |

Set the API key in the connection form (leave blank for local servers that don't require auth), then start a run from **New Experiment**.

## ⚙️ Four ways to run it

There are exactly **four** ways to run Studio. Each is a standard, complete setup — pick the one that fits. All of them give you the same app at <http://localhost:8000> with the AI chat assistant (`/chat/`) on by default, and all of them can turn chat off (see [Chat](#-chat)).

| Way | Standard command(s) | Best for | Runs on |
|-----|---------------------|----------|---------|
| **1. One-liner** | `uvx simpleaudit-studio@latest` | Trying it out; running a stable release | Any machine with [uv](https://docs.astral.sh/uv/) — no Docker |
| **2. Docker** | `docker build -t simpleaudit-studio .`<br>`docker run -d -p 8000:7860 -v sa-data:/data --name simpleaudit-studio simpleaudit-studio` | Self-hosting a single copy (e.g. a demo server) | Docker |
| **3. Compose** | `cp .env.example .env` (edit three values, below)<br>`docker compose up -d` | Teams and production | Docker |
| **4. Development** | `uv run manage.py dev` | Working on the code itself | This checkout, [uv](https://docs.astral.sh/uv/) — no Docker |

> Stuck on which? **One-liner** to try it, **Compose** to run it seriously, **Development** to change it. **Docker** is the middle ground: one container to look after, with everything — including the database — kept inside it.

### 1. One-liner (`uvx simpleaudit-studio`)

```bash
uvx simpleaudit-studio@latest
```

Downloads and runs the latest release in an isolated environment. On first start it creates your admin user, sample scenarios, and model connections, then opens the web app. Your data lives in `~/.simpleaudit-studio/` — back up that folder to back up your work. The `--mock` flag starts a built-in fake model server instead (simulated results, no API key needed).

### 2. Docker (one container)

```bash
git clone https://github.com/SimulaMet/SimpleAuditStudio
cd SimpleAuditStudio
docker build -t simpleaudit-studio .
docker run -d -p 8000:7860 -v sa-data:/data --name simpleaudit-studio simpleaudit-studio
```

Everything (web app, background workers, task queue, chat) runs inside one container. All your data is stored in the `sa-data` volume, so the container can be deleted and recreated at any time without losing anything. Login is `studio` / `admin123` — for anything you share, set your own password: add `-e BOOTSTRAP_PASSWORD=<strong-password>` to the `docker run` line (only works on first start). To update: `docker pull`/rebuild the image, then run the same `docker run` line again.

### 3. Compose (teams and production)

```bash
git clone https://github.com/SimulaMet/SimpleAuditStudio
cd SimpleAuditStudio
cp .env.example .env
# edit .env: DJANGO_SECRET_KEY, POSTGRES_PASSWORD, BOOTSTRAP_PASSWORD
docker compose up -d
```

The full stack: a real database (PostgreSQL), separate background worker, the web app, and the chat service — each in its own container, all managed by one command. Startup refuses to boot until the three values above are set. Your data lives in the Docker volumes `postgres_data` and `open_webui_data`. For upgrades, backups, and security hardening see [Deployment](docs/deployment.md).

### 4. Development (working on the code)

```bash
git clone https://github.com/SimulaMet/SimpleAuditStudio
cd SimpleAuditStudio
cp .env.local.example .env      # local settings; admin is studio / localdevpass123
uv sync --extra dev             # install the app + dev tools once
uv run manage.py setup_local    # first time only: create database + admin + sample data
uv run manage.py dev            # start: web + workers + queue + chat → http://localhost:8000
```

That last command is the **only** way to start the app while developing. It runs your code with live reload (the web app picks up edits automatically), applies the database updates and creates the admin on every start, and opens your browser signed in (`--no-browser` skips the pop-up; it still prints a one-time sign-in link either way). Two things to know:

- Only one Studio can run at a time on a machine (they share one task-queue folder).
- The background worker does not hot-reload: after changing `infra/worker.py` or `infra/engine.py`, restart `uv run manage.py dev`.

Useful flags: `--disable-chat` (start without the chat assistant), `--no-worker` (web UI + API only), and `uv run manage.py mode` (prints exactly which setup is active and with what settings).

#### Tests and lint

```bash
uv run pytest -n auto -m "not slow and not embedded_hatchet"   # fast set, for the dev loop (~1 min)
uv run pytest -n auto -m "not embedded_hatchet"                # full set, before committing
uv run ruff check .                                            # lint
uv run pytest infra/tests/test_workspaces.py::WorkspaceTests::test_create   # a single test
```

`slow` marks the heavy end-to-end tests; the fast command skips them (CI always runs them).

## 💬 Chat

SimpleAudit Studio embeds [Open WebUI](https://openwebui.com) at `/chat/`, signed
in as your Studio user — workspace admins become Open WebUI admins.

Chat is **on by default in every one of the four ways above**. To turn it off,
either pass the flag at start or set the environment variable
(`SIMPLEAUDIT_CHAT=off` in your `.env`) — the flag wins over the file:

```bash
uv run manage.py dev --disable-chat        # development, without chat
uvx simpleaudit-studio --disable-chat      # one-liner, without chat
docker run -e SIMPLEAUDIT_CHAT=off ...     # docker, without chat
# compose: remove SIMPLEAUDIT_CHAT=docker and COMPOSE_PROFILES=chat from .env
```

See [docs/chat.md](docs/chat.md) for how single sign-on works and what must stay
private.

## ✨ What You Can Do

- Build versioned scenario sets and register OpenAI-compatible models
- Define versioned judges: start from a SimpleAudit judge (safety, harm, helpfulness, factuality, abstention, checklist, …) and edit its criteria, or write your own criteria as a severity, 1–10 score (with your own dimensions) or yes/no judge; run with any judge model
- Test your deployment's target system prompt, and compare prompts or judges side by side
- Run experiments across models, scenario versions and settings, with a review step before launch
- Watch runs live, including per-repetition results
- Monitor models for drift on a schedule (interval or cron)
- Filter, customise and compare runs on an interactive dashboard
- Visualize results: browse a folder of `simpleaudit` JSON results in a file-tree viewer, drag-drop a file offline, export a self-contained HTML file, and compare runs with fragility metrics. Run Studio as a visualization-only server with `spin --visualize-only --results_dir ./results`

## 🏗️ Architecture

```
Browser → Django (web app + API)
              ↓
         Database (PostgreSQL in Compose; SQLite everywhere else)
              ↓
         Task queue (Hatchet)
              ↓
         Worker (CPU pool) → SimpleAudit Engine
              Target → Auditor → Judge
```

The exact services (and ports) per setup are listed in [Four ways to run it](#-four-ways-to-run-it).

## 📚 Documentation

- [Architecture](docs/architecture.md) — system design, service boundaries, deployment topology
- [Domain Model](docs/domain-model.md) — scenarios, scenario sets, audit runs, immutability invariants
- [Deployment](docs/deployment.md) — Docker Compose, production hardening, upgrades

## 🤝 Contributing

Architecture decisions are documented in [docs/architecture.md](docs/architecture.md).
