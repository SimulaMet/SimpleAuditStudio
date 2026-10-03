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

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Python](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](https://www.python.org/)
[![PyPI](https://img.shields.io/pypi/v/simpleaudit-studio)](https://pypi.org/project/simpleaudit-studio/)

A self-hostable platform for reproducible AI model audits using the [SimpleAudit](https://github.com/SimulaMet/simpleaudit) engine.

## Quick run

Needs [uv](https://docs.astral.sh/uv/):

```bash
uvx simpleaudit-studio@latest
```

Open <http://localhost:8000>. Try the [live demo](https://sushantgautam-simpleaudit-studio.hf.space) if you do not want to install anything.

## Development

```bash
git clone https://github.com/SimulaMet/SimpleAuditStudio
cd SimpleAuditStudio
cp .env.local.example .env
uv sync --extra dev
uv run manage.py setup_local
uv run manage.py dev
```

The app opens at <http://localhost:8000> with live reload. Use `--no-browser` to skip opening a browser.

## What it does

- Build versioned scenario sets and register OpenAI-compatible models.
- Define judges and run audits across models, scenarios, and settings.
- Review runs live and compare results in the dashboard.
- Monitor models for drift on a schedule.

## Documentation

- [Deployment](docs/deployment.md) — all run commands, settings, chat, upgrades, backups, and production notes
- [Chat](docs/chat.md) — chat setup and privacy details
- [Architecture](docs/architecture.md) — system design and service boundaries
- [Domain model](docs/domain-model.md) — core data and invariants

## Contributing

Architecture decisions are documented in [docs/architecture.md](docs/architecture.md).
