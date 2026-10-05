#!/usr/bin/env bash
# =============================================================================
# Build SimpleAudit Studio's subpath Open WebUI image.
#
# This is the canonical, reproducible build. It clones the pinned upstream
# Open WebUI, applies the subpath patch (verified against the pinned SHA), and
# builds with the upstream project's own Dockerfile — the subpath patch is what
# adds `WEBUI_SUBPATH` as a build/runtime arg to that Dockerfile.
#
# Usage:
#   ./build.sh                       # subpath /chat, tag simpleaudit/open-webui-subpath:chat
#   SUBPATH=/openwebui ./build.sh    # different prefix
#   SHA=<full-sha> ./build.sh        # try a newer upstream (must re-verify patch)
#
# If the patch fails to apply against a newer SHA, the build stops with a clear
# message — do not force it. Rebase the patch (see README.md "Upgrading") first.
# =============================================================================
set -euo pipefail

REPO="https://github.com/open-webui/open-webui.git"
# Pinned upstream commit the patch was verified against (open-webui PR #25590).
SHA="${SHA:-8bd8b4fac5e059578ac0c74b3c18d11139f88b7d}"
SUBPATH="${SUBPATH:-/chat}"
TAG="${TAG:-simpleaudit/open-webui-subpath:chat}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PATCH_SUBPATH="$SCRIPT_DIR/subpath.patch"
PATCH_OTEL="$SCRIPT_DIR/0003-otel-wheel-dependencies.patch"

# Validate subpath format (must match what the patch's env.py expects).
if [[ -n "$SUBPATH" ]]; then
  [[ "$SUBPATH" == /* ]]  || { echo "SUBPATH must start with '/' (got '$SUBPATH')" >&2; exit 1; }
  [[ "$SUBPATH" != */ ]]  || { echo "SUBPATH must not end with '/' (got '$SUBPATH')" >&2; exit 1; }
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
SRC="$WORK/open-webui"

echo "==> Cloning upstream @ ${SHA:0:10} (blobless, sparse)..."
git clone --filter=blob:none --no-checkout "$REPO" "$SRC"
git -C "$SRC" checkout -q "$SHA"
git -C "$SRC" clean -fdx

echo "==> Applying subpath patch..."
git -C "$SRC" apply --check "$PATCH_SUBPATH"
git -C "$SRC" apply "$PATCH_SUBPATH"

echo "==> Adding OTel dependencies to wheel metadata..."
git -C "$SRC" apply --check "$PATCH_OTEL"
git -C "$SRC" apply "$PATCH_OTEL"

echo "==> Building image ${TAG} (WEBUI_SUBPATH=${SUBPATH})..."
# Build context is the checked-out tree; the patched Dockerfile is inside it.
docker build \
  -f "$SRC/Dockerfile" \
  --build-arg "WEBUI_SUBPATH=${SUBPATH}" \
  --build-arg "BUILD_HASH=${SHA:0:12}" \
  -t "$TAG" \
  "$SRC"

echo
echo "Built ${TAG}. Run + route it with:"
echo "  docker run -d --name owui -p 8090:8080 -e WEBUI_SUBPATH=${SUBPATH} ${TAG}"
echo "  then point Caddy at it: see $SCRIPT_DIR/Caddyfile and README.md"
