# ══════════════════════════════════════════════════════════════
# Dr. Maheshwari — AI Professor Agent
# All-in-one image: Ollama + baked model + Tesseract OCR + FastAPI
# ══════════════════════════════════════════════════════════════
# Build:  docker build -t dr-maheshwari .
# Run:    docker run -p 8000:8000 dr-maheshwari
#
# NOTE (Apple Silicon / Mac): Ollama runs CPU-only inside Docker on macOS
# (no Metal GPU passthrough). Use native Ollama on the Mac for best speed;
# use this image on a Linux host (ideally GPU) for portable deployment.
# ──────────────────────────────────────────────────────────────

# Base already contains the Ollama runtime (multi-arch: amd64 + arm64)
FROM ollama/ollama:latest

# ── System deps: Python + Tesseract (OCR) + curl (healthcheck) ──
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        python3-venv \
        tesseract-ocr \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Python environment (venv avoids PEP-668 issues cleanly) ──
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# ── Application code ──
COPY main.py .
COPY frontend/ ./frontend/

# ── Bake the model INTO the image ──
# Spin up ollama briefly during build, pull the model, then stop.
# Override MODEL at build time:  docker build --build-arg MODEL=qwen2.5:3b .
ARG MODEL=gemma2:2b
RUN ollama serve & \
    sleep 8 && \
    ollama pull "${MODEL}" && \
    (pkill ollama || true)

# ── Runtime configuration ──
ENV OLLAMA_URL=http://127.0.0.1:11434 \
    MODEL_FAST=gemma2:2b \
    MODEL_DEEP= \
    VISION_MODEL= \
    OCR_ENABLED=true \
    CONTEXT_WINDOW=4096 \
    MAX_TOKENS_FAST=1500 \
    MAX_CONCURRENT=3 \
    TEACHER_PASSWORD=njr2026 \
    PORT=8000

EXPOSE 8000

# Basic container healthcheck against the app's /health endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -sf http://127.0.0.1:8000/health || exit 1

# ── Entrypoint: start Ollama, wait, then launch the API ──
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh
ENTRYPOINT ["/docker-entrypoint.sh"]
