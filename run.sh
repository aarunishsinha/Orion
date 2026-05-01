#!/bin/bash
# Launch Orion — resolves paths so it can be run from anywhere.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/client_hud" && uv run python ../orchestrator/orchestrator_v3.py "$@"
