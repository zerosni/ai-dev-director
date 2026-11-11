#!/usr/bin/env sh
# Collaboration environment check for AI DEV DIRECTOR (HEAD=claude, BUILDER=agy, REVIEWER=codex).
set -e
cd "$(dirname "$0")"
pip install -q -r requirements.txt
mkdir -p data
ok=0
for c in git claude codex agy; do
  if command -v "$c" >/dev/null 2>&1; then echo "[OK]      $c -> $(command -v $c)"; ok=$((ok+1))
  else echo "[MISSING] $c"; fi
done
echo "API keys are stripped by core/env_utils.py; use each CLI's subscription login (claude / codex login / agy)."
echo "Need >=2 CLIs for collaboration. If agy is missing, set builder.provider to codex or claude in config.yaml."
echo "Start console: ./start-companion.sh  -> http://127.0.0.1:8081"
