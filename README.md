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
uvx simpleaudit-studio@latest
```

[`uvx`](https://docs.astral.sh/uv/#uvx) installs the latest [`simpleaudit-studio`](https://pypi.org/project/simpleaudit-studio/) package and runs it in an isolated environment. No Docker, no Postgres, no manual setup. Opens at http://localhost:8000 (login: `studio` / `admin123`). A mock model server is pre-seeded so you can start exploring audit results immediately. Your data is kept in `~/.simpleaudit-studio/` (set `SIMPLEAUDIT_DATA_DIR` to change it), so it survives restarts and upgrades.

🌐 Or skip the setup entirely — try the live demo: <a href="https://sushantgautam-simpleaudit-studio.hf.space" target="_blank" rel="noopener noreferrer">sushantgautam-simpleaudit-studio.hf.space</a>

Want the **latest unreleased code** from this repo instead of the PyPI release? Same one-liner, pointed at git:

```bash
uvx --from "git+https://github.com/SushantGautam/SimpleAuditStudio@main" spin
```


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

## 🛠️ Local Development

Needs Python 3.11+ and [uv](https://docs.astral.sh/uv/). No Docker, no Makefile.

### Setup (once)

```bash
git clone https://github.com/SushantGautam/SimpleAuditStudio
cd SimpleAuditStudio
cp .env.local.example .env        # SQLite + embedded queue; admin is studio / BOOTSTRAP_PASSWORD
uv sync --extra dev               # create .venv with app + dev tools
uv run manage.py setup_local      # migrate, create admin + workspace, seed scenario packs & models
```

### Daily work

```bash
uv run manage.py dev_server --embedded    # web UI + API + worker + embedded Hatchet → http://localhost:8000
```

Every start applies migrations and makes sure the admin (a superuser) and default workspace exist, so pulling new code needs no extra steps. Keep in mind:

- Only one `dev_server --embedded` (or `uvx simpleaudit-studio`) can run at a time: they share the embedded queue database.
- The web server reloads on code changes; the worker does not. Restart `dev_server` after changing `infra/worker.py` or `infra/engine.py`.

### Tests and lint

Tests run under **pytest** (via `pytest-django`). Your existing
`django.test.TestCase` classes run unchanged. Tests are layered: run the
**fast** set while coding (skips the slow integration modules tagged `slow`),
and the **full** set before you commit or open a PR.

```bash
# Fast — unit + light integration, for the dev loop (~50s, skips the slow modules)
uv run pytest -n auto -m "not slow and not embedded_hatchet"

# Full — everything, for before commit / PR
uv run pytest -n auto -m "not embedded_hatchet"

# Affected-only — run just the tests touched by your changed code (needs a
# prior run to build .testmondata; CI caches it)
uv run pytest --testmon -n auto -m "not slow and not embedded_hatchet"

# Lint
uv run ruff check .

# Target a single module, class, or test to iterate faster
uv run pytest infra/tests/test_workspaces.py              # one module
uv run pytest infra/tests/test_workspaces.py::WorkspaceTests  # one class
uv run pytest infra/tests/test_workspaces.py::WorkspaceTests::test_create  # one test
```

**Tagging**

- `slow` — heavy integration modules (experiments, monitors, judges, engine
  integration, full API lifecycle). Skipped by the fast command, always run in
  CI. Add `@tag("slow")` to a class to move a slow test out of the fast loop.
- `embedded_hatchet` — starts a real embedded Hatchet worker; runs serially in
  CI only when the relevant files change.

The Django `@tag("...")` values are mirrored onto pytest markers by
`conftest.py`, so `-m "not slow"` works the same as
`manage.py test --exclude-tag slow`.

### Other setups

```bash
uv run manage.py dev_server --no-worker   # web UI + API only
uv run manage.py dev_server               # use the Postgres + Hatchet configured in .env (see .env.example)
```

See [docs/deployment.md](docs/deployment.md) for every environment variable and management command.

## 🐳 Self-Hosting

For teams or multi-user setups, use Docker Compose:

```bash
git clone https://github.com/SushantGautam/SimpleAuditStudio
cd SimpleAuditStudio
cp .env.example .env
# edit DJANGO_SECRET_KEY, POSTGRES_PASSWORD and BOOTSTRAP_PASSWORD at minimum —
# startup refuses to boot while any of them is empty or still `change-me`
docker compose up -d
```

Services: Web UI (:8000), PostgreSQL, Hatchet queue (:8888), Worker. Chat (Open WebUI) is included via `.env`; optional profile `--profile mock` adds a mock model API.

See [docs/deployment.md](docs/deployment.md) for production hardening, backups, and upgrades.

## 💬 Chat

SimpleAudit Studio embeds [Open WebUI](https://openwebui.com) at `/chat/`, signed
in as your Studio user — workspace admins become Open WebUI admins.

Chat is opt-in. The local one-liner bundles it by default (pass
`--disable-chat` to turn it off); Docker Compose leaves it out unless `.env`
says otherwise — uncomment `SIMPLEAUDIT_CHAT` and `COMPOSE_PROFILES` in
`.env.example` to include it:

```bash
uvx simpleaudit-studio                 # chat included
uvx simpleaudit-studio --disable-chat  # without it

docker compose up -d                   # chat only if enabled in .env
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

## 🏗️ Architecture

```
Browser → Django (gunicorn :8000 in Compose; runserver :7860 in the HF Space / uvx demo)
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
- [Deployment](docs/deployment.md) — Docker Compose, production hardening, upgrades

## 🤝 Contributing

Architecture decisions are documented in [docs/architecture.md](docs/architecture.md).
