#!/usr/bin/env bash
# install-yard.sh — put the `yard` host CLI on PATH so `which yard` resolves.
#
# The box is the source of truth for origins; `yard connect <host>` registers
# one host-side (the web only mirrors). This installs a symlink from a PATH dir
# to the repo's bin/yard shim — no copy, so it tracks the checkout.
#
# Usage:  scripts/install-yard.sh [target-bin-dir]   (default: ~/.local/bin)
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$REPO/bin/yard"
DEST_DIR="${1:-$HOME/.local/bin}"

[ -x "$SRC" ] || { echo "install-yard: $SRC not found or not executable" >&2; exit 1; }
mkdir -p "$DEST_DIR"
ln -sf "$SRC" "$DEST_DIR/yard"

echo "installed: $DEST_DIR/yard -> $SRC"
case ":$PATH:" in
  *":$DEST_DIR:"*) echo "on PATH — run: yard help" ;;
  *) echo "note: $DEST_DIR is not on PATH; add it, then run: yard help" ;;
esac
