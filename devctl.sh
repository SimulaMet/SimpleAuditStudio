#!/usr/bin/env bash
# devctl.sh — stable start/stop for the SimpleAudit local dev stack.
# Usage: ./devctl.sh {start|stop|status|restart}
#
# Requirements:
#   - SIMULACHAT_API_KEY or CUSTOM_SIMULACHAT_KEY in environment (LLM access)
#   - Ollama running on :11434 (local LLM fallback) OR a cloud LLM endpoint
#   - The OpenAI model connection (id=1) in dev.sqlite3 must point to a
#     working base_url + api_key_direct (or secret_reference env var name)
#
# DB: uses dev.sqlite3 (same as `manage.py dev` reads from .env)
# Ports: Studio :8001, OWUI :8080, Caddy :8000
set -euo pipefail

# Compatibility wrapper delegating to the ownership-safe lifecycle manager.
# The legacy kill-by-port cleanup was removed: no path here may terminate a
# process by port, name, or broad pattern (another agent's worktree included).
_REPO="$(cd "$(dirname "$0")" && pwd)"
case "${1:-}" in
  start)
    mkdir -p "$HOME/.local/state/simpleaudit-studio"
    (cd "$_REPO" && nohup uv run manage.py dev --no-browser \
      >> "$HOME/.local/state/simpleaudit-studio/devctl.log" 2>&1 </dev/null &)
    exit 0 ;;
  stop)    cd "$_REPO"; exec uv run manage.py dev down ;;
  status)  cd "$_REPO"; exec uv run manage.py dev status ;;
  restart) cd "$_REPO"; exec uv run manage.py dev restart --no-browser ;;
  *) echo "Usage: $0 {start|stop|status|restart}"; exit 2 ;;
esac
