#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════
# Container entrypoint for Dr. Maheshwari
# Starts Ollama in the background, waits for it, then runs the API.
# ══════════════════════════════════════════════════════════════
set -e

echo "──────────────────────────────────────────────"
echo "  🎓 Dr. Maheshwari — AI Professor Agent"
echo "  Starting Ollama + FastAPI inside container"
echo "──────────────────────────────────────────────"

# 1) Start the Ollama server in the background
ollama serve &

# 2) Wait until Ollama answers before starting the app
echo "⏳ Waiting for Ollama to be ready..."
until curl -sf http://127.0.0.1:11434/api/tags >/dev/null 2>&1; do
    sleep 1
done
echo "✅ Ollama is ready."

# 3) Make sure the teaching model is present.
#    (Baked in at build time; this also covers the case of a mounted
#     model volume that doesn't yet contain it.)
MODEL="${MODEL_FAST:-gemma2:2b}"
if ! ollama list | grep -q "${MODEL%%:*}"; then
    echo "⬇️  Pulling ${MODEL} (not found locally)..."
    ollama pull "${MODEL}" || echo "⚠️  Could not pull ${MODEL} (no network?). Continuing."
fi

# 4) Launch the FastAPI app in the foreground (becomes the main process)
echo "🚀 Launching API on port ${PORT:-8000}"
exec python -m uvicorn main:app \
    --host 0.0.0.0 \
    --port "${PORT:-8000}" \
    --timeout-keep-alive 300 \
    --log-level info
