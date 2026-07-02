#!/bin/bash
# =============================================================================
# BuckGen — Render Startup Script
# =============================================================================

set -e

echo "[start] Checking data directory..."
mkdir -p /app/data

echo "[start] Starting BuckGen agent..."
exec uvicorn app.main:app \
    --host 0.0.0.0 \
    --port "${PORT:-8000}" \
    --workers 1 \
    --log-level info
