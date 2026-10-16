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

PORT_STUDIO=8001
PORT_OWUI=8080
PORT_CADDY=8000
PIDFILE="$HOME/.simpleaudit-studio/dev.pid"
LOGFILE="$HOME/.simpleaudit-studio/dev.log"
DB="dev.sqlite3"

REPO="$(cd "$(dirname "$0")" && pwd)"

_kill_port() {
  local port=$1
  local pids
  pids=$(lsof -ti :"$port" 2>/dev/null || true)
  if [[ -n "$pids" ]]; then
    # shellcheck disable=SC2086
    kill -9 $pids 2>/dev/null || true
  fi
}

stop() {
  echo "Stopping stack (ports $PORT_STUDIO $PORT_OWUI $PORT_CADDY)..."
  for p in "$PORT_STUDIO" "$PORT_OWUI" "$PORT_CADDY"; do _kill_port "$p"; done
  # Also kill any lingering manage.py dev processes
  local mpids
  mpids=$(pgrep -f "manage.py dev" 2>/dev/null || true)
  if [[ -n "$mpids" ]]; then
    # shellcheck disable=SC2086
    kill -9 $mpids 2>/dev/null || true
  fi
  rm -f "$PIDFILE"
  sleep 2
  # Verify
  local remaining
  remaining=$(lsof -ti :$PORT_STUDIO,$PORT_OWUI,$PORT_CADDY 2>/dev/null || true)
  if [[ -n "$remaining" ]]; then
    echo "WARNING: ports still occupied: $remaining"
  else
    echo "Stopped."
  fi
}

start() {
  if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "Already running (PID $(cat "$PIDFILE")). Use 'restart' to restart."
    return 0
  fi
  stop  # ensure clean slate

  # Clear stale bytecode so code changes are always picked up
  find "$REPO" -path "*__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

  echo "Starting stack (Studio :$PORT_STUDIO, OWUI :$PORT_OWUI, Caddy :$PORT_CADDY)..."
  mkdir -p "$(dirname "$LOGFILE")"

  cd "$REPO"
  export SIMULACHAT_API_KEY="${SIMULACHAT_API_KEY:-${CUSTOM_SIMULACHAT_KEY:-}}"
  SIMPLEAUDIT_LOCAL_SQLITE=1 \
  SIMPLEAUDIT_SQLITE_PATH="$DB" \
  SIMPLEAUDIT_BROWSER_PORT="$PORT_CADDY" \
  nohup uv run manage.py dev --port "$PORT_STUDIO" > "$LOGFILE" 2>&1 &
  local pid=$!
  echo "$pid" > "$PIDFILE"
  echo "Started (PID $pid). Log: $LOGFILE"

  # Wait for health
  local i
  for i in $(seq 1 40); do
    if curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$PORT_STUDIO/healthz" 2>/dev/null | grep -q 200; then
      echo "Studio healthy."
      local owui
      owui=$(curl -s "http://127.0.0.1:$PORT_OWUI/chat/health" 2>/dev/null || echo "")
      local caddy
      caddy=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$PORT_CADDY/" 2>/dev/null || echo "000")
      echo "OWUI: ${owui:-down}  Caddy(8000): HTTP $caddy"
      return 0
    fi
    sleep 2
  done
  echo "ERROR: Studio did not become healthy in 80s. Check $LOGFILE"
  return 1
}

status() {
  local h
  h=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$PORT_STUDIO/healthz" 2>/dev/null || echo "000")
  local o
  o=$(curl -s "http://127.0.0.1:$PORT_OWUI/chat/health" 2>/dev/null || echo "down")
  local c
  c=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$PORT_CADDY/" 2>/dev/null || echo "000")
  echo "Studio(:$PORT_STUDIO): HTTP $h"
  echo "OWUI(:$PORT_OWUI):     $o"
  echo "Caddy(:$PORT_CADDY):   HTTP $c"
  if [[ -f "$PIDFILE" ]]; then
    echo "PID file: $(cat "$PIDFILE")"
  fi
}

case "${1:-}" in
  start) start ;;
  stop) stop ;;
  status) status ;;
  restart) stop; start ;;
  *) echo "Usage: $0 {start|stop|status|restart}"; exit 1 ;;
esac
