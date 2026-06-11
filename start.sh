#!/bin/bash
# ═══════════════════════════════════════════════════════
# Dr. Maheshwari — AI Professor Agent v3
# One-click launcher for Mac Mini M4 (16GB RAM)
# ═══════════════════════════════════════════════════════

set -e

R='\033[0;31m'; G='\033[0;32m'; Y='\033[0;33m'; B='\033[0;34m'; P='\033[0;35m'; N='\033[0m'
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

echo ""
echo -e "  ${Y}╔══════════════════════════════════════════════╗${N}"
echo -e "  ${Y}║${N}  🎓  ${B}Dr. Maheshwari — AI Professor Agent v3${N}   ${Y}║${N}"
echo -e "  ${Y}║${N}  📍  NJR i3 Labs · Techno NJR Udaipur       ${Y}║${N}"
echo -e "  ${Y}║${N}  💻  Mac Mini M4 · 16GB · No Docker          ${Y}║${N}"
echo -e "  ${Y}╚══════════════════════════════════════════════╝${N}"
echo ""

# Load .env
if [ -f .env ]; then
    export $(grep -v '^#' .env | grep -v '^\s*$' | xargs)
    echo -e "  ${G}✓${N} Loaded .env"
fi

# Defaults
OLLAMA_URL="${OLLAMA_URL:-http://127.0.0.1:11434}"
MODEL_FAST="${MODEL_FAST:-gemma2:2b}"
MODEL_DEEP="${MODEL_DEEP:-}"
VISION_MODEL="${VISION_MODEL:-llava:7b}"
PORT="${PORT:-8000}"

# Check Python
if ! command -v python3 &>/dev/null; then
    echo -e "  ${R}✗${N} Python3 not found. Install it first."
    exit 1
fi
echo -e "  ${G}✓${N} Python $(python3 --version 2>&1 | cut -d' ' -f2)"

# Virtual environment
if [ -d "venv" ]; then
    source venv/bin/activate
    echo -e "  ${G}✓${N} Activated venv"
else
    echo -e "  ${Y}→${N} Creating virtual environment..."
    python3 -m venv venv
    source venv/bin/activate
    pip install -q --upgrade pip
    pip install -q -r requirements.txt
    echo -e "  ${G}✓${N} Created venv + installed dependencies"
fi

# Check Tesseract (OCR for screenshot reading)
echo ""
echo -e "  ${B}Checking OCR (screenshot reading)...${N}"
if command -v tesseract > /dev/null 2>&1; then
    echo -e "  ${G}✓${N} Tesseract installed ($(tesseract --version 2>&1 | head -1))"
else
    echo -e "  ${Y}⚠${N}  Tesseract not found — screenshot text reading will be limited."
    echo -e "     Install it for full image support:  ${B}brew install tesseract${N}"
fi

# Check Ollama
echo ""
echo -e "  ${B}Checking Ollama...${N}"
if curl -s "$OLLAMA_URL/api/tags" > /dev/null 2>&1; then
    echo -e "  ${G}✓${N} Ollama is running"
else
    echo -e "  ${R}✗${N} Ollama not running! Start it:"
    echo -e "     ${Y}ollama serve${N}"
    exit 1
fi

# Check models
MODELS=$(curl -s "$OLLAMA_URL/api/tags" | python3 -c "import sys,json;[print(m['name'])for m in json.load(sys.stdin).get('models',[])]" 2>/dev/null)

check_model() {
    local name=$1 required=$2
    if echo "$MODELS" | grep -q "$name"; then
        SIZE=$(echo "$MODELS" | grep "$name" | head -1)
        echo -e "  ${G}✓${N} $name → ready"
        return 0
    else
        if [ "$required" = "yes" ]; then
            echo -e "  ${R}✗${N} $name → NOT FOUND"
            echo -e "     Run: ${Y}ollama pull $name${N}"
            return 1
        else
            echo -e "  ${Y}○${N} $name → optional (not installed)"
            return 0
        fi
    fi
}

check_model "$MODEL_FAST" "yes" || exit 1
[ -n "$MODEL_DEEP" ] && check_model "$MODEL_DEEP" "no"
echo ""

# Check vision model
echo -e "  ${P}Checking vision model...${N}"
if echo "$MODELS" | grep -q "$VISION_MODEL"; then
    echo -e "  ${G}✓${N} $VISION_MODEL → 📸 Screenshot analysis enabled!"
else
    echo -e "  ${Y}⚠${N} $VISION_MODEL not found"
    echo -e "     Screenshot analysis will use text-only fallback."
    echo -e "     For full image analysis: ${Y}ollama pull $VISION_MODEL${N}"
    echo -e "     ${Y}NOTE:${N} llava:7b needs ~4.5GB RAM. Only install if you"
    echo -e "     have enough free RAM (disable deep model if needed)."
fi

# RAM estimate
echo ""
echo -e "  ${B}RAM Budget (16GB total):${N}"
echo -e "  ├── macOS          ~4.0 GB"
echo -e "  ├── FastAPI+Python ~0.3 GB"
echo -e "  ├── $MODEL_FAST     ~1.5 GB"
if [ -n "$MODEL_DEEP" ] && echo "$MODELS" | grep -q "$MODEL_DEEP"; then
echo -e "  ├── $MODEL_DEEP  ~2.5 GB"
fi
if echo "$MODELS" | grep -q "$VISION_MODEL"; then
echo -e "  ├── $VISION_MODEL     ~4.5 GB (loaded on demand)"
fi
echo -e "  └── Free for students ~7-8 GB"
echo ""

# Get IP
IP=$(ifconfig 2>/dev/null | grep 'inet ' | grep -v '127.0.0.1' | head -1 | awk '{print $2}')
IP=${IP:-localhost}

echo "───────────────────────────────────────────────────"
echo -e "  ${G}🚀 Starting Dr. Maheshwari!${N}"
echo ""
echo -e "  ${Y}Student URL:${N}  http://${IP}:${PORT}"
echo -e "  ${Y}Teacher URL:${N}  http://${IP}:${PORT}/teacher"
echo -e "  ${Y}Teacher pwd:${N}  ${TEACHER_PASSWORD:-njr2026}"
echo -e "  ${Y}Health:${N}       http://${IP}:${PORT}/health"
echo ""
echo -e "  Press ${R}Ctrl+C${N} to stop"
echo "───────────────────────────────────────────────────"
echo ""

OLLAMA_URL="$OLLAMA_URL" \
MODEL_FAST="$MODEL_FAST" \
MODEL_DEEP="$MODEL_DEEP" \
VISION_MODEL="$VISION_MODEL" \
MAX_CONCURRENT="${MAX_CONCURRENT:-3}" \
MAX_TOKENS_FAST="${MAX_TOKENS_FAST:-1500}" \
MAX_TOKENS_DEEP="${MAX_TOKENS_DEEP:-2000}" \
MAX_TOKENS_VISION="${MAX_TOKENS_VISION:-1500}" \
CONTEXT_WINDOW="${CONTEXT_WINDOW:-4096}" \
REQUEST_TIMEOUT="${REQUEST_TIMEOUT:-300}" \
TEACHER_PASSWORD="${TEACHER_PASSWORD:-njr2026}" \
CACHE_SIZE="${CACHE_SIZE:-200}" \
RATE_LIMIT="${RATE_LIMIT:-10}" \
python3 -m uvicorn main:app \
    --host 0.0.0.0 \
    --port "$PORT" \
    --workers 1 \
    --timeout-keep-alive 300 \
    --log-level info
