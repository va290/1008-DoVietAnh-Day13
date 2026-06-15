#!/usr/bin/env bash
# Run the Observathon sim against OmniRoute (the Day03/04/09/11 OpenAI-compatible
# gateway) from this Ubuntu-20.04 host.
#
# Why Docker: the sim binary is a PyInstaller bundle that needs GLIBC >= 2.38,
# but this host has 2.31. python:3.12-slim gives a new-enough glibc AND a full
# Python 3.12 stdlib + pip, which the bundle needs (it ships without `openai`/
# `asyncio`; solution/wrapper.py injects them onto sys.path at import time).
#
# Usage:
#   ./run.sh                      # practice phase, seed 7, fixed public-ish traffic
#   PHASE=practice SEED=7 ./run.sh
#   ./run.sh --users 200 --turns 12 --concurrency 12   # extra args pass through
set -euo pipefail
cd "$(dirname "$0")"

# shellcheck disable=SC1091
[ -f .env ] && set -a && . ./.env && set +a

PHASE="${PHASE:-practice}"
SEED="${SEED:-7}"
CONFIG="${CONFIG:-solution/config.json}"
WRAPPER="${WRAPPER:-solution/wrapper.py}"
# Scratch/debug runs land in runs/ (gitignored). For the final submission run,
# set OUT=run_output.json (SUBMIT.md expects it at repo root).
mkdir -p runs
OUT="${OUT:-runs/${PHASE}.json}"
BIN="bin/${PHASE}/observathon-sim"
# practice uses its own seed flag; public/private use the fixed --testset set.
if [ "$PHASE" = "practice" ]; then
  PHASE_FLAG="--practice"
else
  PHASE_FLAG="--testset $PHASE"
fi

# ENGINE=omni (default)  -> OmniRoute gateway (config model = claude/claude-haiku-...).
# ENGINE=openai          -> real OpenAI api.openai.com (set config model = gpt-4o-mini;
#                           cheaper/faster but costs real money).
ENGINE="${ENGINE:-openai}"
if [ "$ENGINE" = "omni" ]; then
  ENGINE_KEY="${OMNI_API_KEY:?set OMNI_API_KEY in .env}"
  ENGINE_BASE="${OMNI_BASE_URL:-http://localhost:20128/v1}"
else
  ENGINE_KEY="${OPENAI_API_KEY:?set OPENAI_API_KEY in .env}"
  ENGINE_BASE=""   # empty -> openai SDK uses the real api.openai.com
fi
BASE_ENV=()
[ -n "$ENGINE_BASE" ] && BASE_ENV=(-e "OPENAI_BASE_URL=$ENGINE_BASE")

# LANGFUSE=1 ./run.sh  -> after the run, push telemetry to Langfuse Cloud
# (needs LANGFUSE_* in .env). The sim always logs to the reliable file backend;
# Langfuse export runs off the agent's critical path.
LANGFUSE="${LANGFUSE:-0}"
PIP_PKGS='openai<2'
[ "$LANGFUSE" = "1" ] && PIP_PKGS="$PIP_PKGS langfuse"

echo "[run.sh] phase=$PHASE engine=$ENGINE out=$OUT wrapper=$WRAPPER"
docker run --rm --network host \
  -v "$PWD":/work -w /work \
  -e OPENAI_API_KEY="$ENGINE_KEY" \
  ${BASE_ENV[@]+"${BASE_ENV[@]}"} \
  -e OBS_BACKEND=file \
  -e WRAP_NO_GUARDRAIL="${WRAP_NO_GUARDRAIL:-}" \
  -e LANGFUSE="$LANGFUSE" -e PHASE="$PHASE" \
  -e LANGFUSE_PUBLIC_KEY="${LANGFUSE_PUBLIC_KEY:-}" \
  -e LANGFUSE_SECRET_KEY="${LANGFUSE_SECRET_KEY:-}" \
  -e LANGFUSE_HOST="${LANGFUSE_HOST:-https://cloud.langfuse.com}" \
  -e PIP_PKGS="$PIP_PKGS" \
  python:3.12-slim bash -c '
    set -e
    pip install --quiet --target /opt/pylibs $PIP_PKGS >/dev/null 2>&1
    export PYTHONPATH=/opt/pylibs:${PYTHONPATH:-}
    "$@"
    if [ "$LANGFUSE" = "1" ]; then
      echo "[run.sh] exporting telemetry to Langfuse Cloud..."
      python tools/export_to_langfuse.py --logs logs --session "$PHASE" || true
    fi
  ' _ "./$BIN" $PHASE_FLAG --config "$CONFIG" --wrapper "$WRAPPER" \
       --out "$OUT" --seed "$SEED" "$@"
