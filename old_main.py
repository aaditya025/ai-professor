"""
Dr. Maheshwari — AI Professor Agent v3
========================================
Mac Mini M4 (16GB RAM) · 50 Students · No Docker · No LangChain

Features:
  ✓ Socratic teaching with code examples + challenges
  ✓ Image/screenshot upload — OCR + AI error analysis
  ✓ Mandatory student registration (name required)
  ✓ Teacher dashboard (live student tracking)
  ✓ Multi-model routing (fast 2B + optional deep 3B)
  ✓ Async semaphore queue (3 concurrent, 50 max)
  ✓ LRU response cache with TTL
  ✓ Per-IP rate limiting
  ✓ Conversation memory per session
  ✓ SSE streaming responses

Architecture:
  Students → POST /register → POST /chat (text) or /chat/image (screenshots)
  Teacher  → GET /teacher (dashboard UI) or /teacher/api (JSON)

Author: Dr. Aaditya Maheshwari | NJR i3 Labs Pvt. Ltd.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
import os
import re
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import AsyncGenerator, Optional

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator

# ═══════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════

OLLAMA_BASE_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
MODEL_FAST = os.getenv("MODEL_FAST", "gemma2:2b")
MODEL_DEEP = os.getenv("MODEL_DEEP", "")
VISION_MODEL = os.getenv("VISION_MODEL", "llava:7b")

MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "3"))
MAX_QUEUE_SIZE = int(os.getenv("MAX_QUEUE", "50"))
MAX_TOKENS_FAST = int(os.getenv("MAX_TOKENS_FAST", "1500"))
MAX_TOKENS_DEEP = int(os.getenv("MAX_TOKENS_DEEP", "2000"))
MAX_TOKENS_VISION = int(os.getenv("MAX_TOKENS_VISION", "1500"))
CACHE_SIZE = int(os.getenv("CACHE_SIZE", "200"))
CACHE_TTL = int(os.getenv("CACHE_TTL", "300"))
RATE_LIMIT_PER_MIN = int(os.getenv("RATE_LIMIT", "10"))
CONTEXT_WINDOW = int(os.getenv("CONTEXT_WINDOW", "4096"))
MAX_IMAGE_SIZE_MB = int(os.getenv("MAX_IMAGE_SIZE_MB", "5"))
# OCR: extract text from screenshots WITHOUT loading a 4.5GB vision model.
# Best path for error/code screenshots (which are mostly text) on a 16GB Mac.
OCR_ENABLED = os.getenv("OCR_ENABLED", "true").lower() == "true"
TEACHER_PASSWORD = os.getenv("TEACHER_PASSWORD", "njr2026")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("dr-maheshwari")

# ═══════════════════════════════════════════════
# OCR ENGINE — lightweight screenshot text extraction
# ═══════════════════════════════════════════════
# Uses Tesseract (via pytesseract) to pull text out of error/code
# screenshots. This is the PRIMARY image path because:
#   • ~50MB RAM vs ~4.5GB for the llava vision model
#   • Runs on CPU in <1s — no model swap
#   • Error messages & code ARE text, so OCR captures exactly what matters
# The extracted text is then fed to gemma2:2b (already loaded) for help.
# If Tesseract isn't installed, we degrade gracefully to vision or text.

_OCR_READY = False
_OCR_REASON = ""
try:
    if OCR_ENABLED:
        import pytesseract
        from PIL import Image, ImageOps, ImageFilter
        import io as _io
        # Cap decoded image size to thwart decompression bombs (a tiny file that
        # expands to enormous dimensions and exhausts RAM). 25M px ≈ 5000×5000.
        Image.MAX_IMAGE_PIXELS = 25_000_000
        # Probe the tesseract binary (pip package needs the system binary)
        _ver = pytesseract.get_tesseract_version()
        _OCR_READY = True
        _OCR_REASON = f"tesseract {_ver}"
    else:
        _OCR_REASON = "disabled via OCR_ENABLED=false"
except ImportError:
    _OCR_REASON = "pytesseract/Pillow not installed (pip install pytesseract pillow)"
except Exception as _e:
    # Most common: tesseract binary missing → 'brew install tesseract'
    _OCR_REASON = f"tesseract binary not found ({_e}); run: brew install tesseract"


def ocr_extract_text(image_bytes: bytes) -> tuple[str, str]:
    """
    Extract text from a screenshot. Returns (text, status).
    status: 'ok' | 'empty' | 'unavailable' | 'error'
    Light preprocessing (grayscale + upscale + sharpen) boosts accuracy
    on dark-theme code editors and small terminal fonts.
    """
    if not _OCR_READY:
        return "", "unavailable"
    try:
        img = Image.open(_io.BytesIO(image_bytes))
        # Normalize orientation from EXIF if present
        img = ImageOps.exif_transpose(img)
        # Convert to grayscale — better for monospaced code/terminal text
        img = img.convert("L")
        # Upscale small screenshots so glyphs are big enough for Tesseract
        w, h = img.size
        if max(w, h) < 1400:
            scale = 1400 / max(w, h)
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        # Sharpen to crisp up anti-aliased editor fonts
        img = img.filter(ImageFilter.SHARPEN)
        # autocontrast helps low-contrast dark themes
        img = ImageOps.autocontrast(img)
        # PSM 6: assume a uniform block of text (good for code/errors)
        text = pytesseract.image_to_string(img, config="--psm 6")
        text = text.strip()
        if len(text) < 3:
            return "", "empty"
        return text, "ok"
    except Exception as e:
        log.error(f"OCR failed: {e}")
        return "", "error"



SYSTEM_PROMPT = """You are Dr. Maheshwari — an AI Professor at Techno NJR Institute of Technology, Udaipur.
You teach the "Vibe Coding" class: Python and AI-assisted software development.

═══════════════════════════════════════════════
FIRST, READ THE STUDENT'S MESSAGE AND PICK ONE MODE:
═══════════════════════════════════════════════

MODE A — GREETING / CHIT-CHAT
  ("hi", "hello", "thanks", "how are you", "good morning")
  → Reply warmly in 1-2 sentences. NO code. NO challenge.
  → Invite their question.
  Example:
    Student: "hi"
    You: "Hello! 👋 I'm Dr. Maheshwari, your Python & Vibe Coding guide. What are you working on today?"

MODE B — CONCEPTUAL QUESTION
  ("what is X", "why does Y happen", "difference between A and B", "when do I use Z")
  → Explain clearly in plain language, with a simple analogy if it helps.
  → Add a SHORT code snippet ONLY if it truly makes the idea clearer.
  → A practice challenge is OPTIONAL — add one only if it fits naturally.
  → Do NOT force code or a challenge if the question is purely conceptual.
  Example:
    Student: "what is a variable?"
    You: "A variable is just a labelled box that stores a value so you can reuse it later. 📦
    For example, `age = 21` puts the number 21 into a box named `age`. Later you can write
    `print(age)` and Python fetches what's inside. Want to try creating a few variables?"

MODE C — CODING / HOW-TO / DEBUGGING
  ("how do I...", "write a program that...", "fix this error", "my code doesn't work", or a pasted error/code)
  → THIS is when you teach with the full flow:
     1. Briefly explain the idea or what went wrong.
     2. Show working, commented code in a ```python block.
     3. End with a short challenge (🧪) and a hint (💡) to reinforce learning.
  → For a bug: praise what works ✅, show the corrected code 🔧, explain WHY.

MODE D — OFF-TOPIC (NOT about Python, programming, AI/ML, prompt engineering, or Vibe Coding)
  (cooking, sports, politics, relationships, medical/legal advice, general trivia, homework
   in other subjects, etc.)
  → Politely decline. Say clearly that you are not trained on this. Redirect to coding.
  → Do NOT attempt an answer. Keep it to 1-2 sentences.
  Example:
    Student: "who won the cricket match yesterday?"
    You: "I'm not trained on that — I'm Dr. Maheshwari, here only to help you with Python and
    Vibe Coding. 😊 Do you have a coding question I can help with?"
    Student: "give me a recipe for biryani"
    You: "That's outside what I'm trained on — I focus only on Python and AI-assisted coding.
    Shall we write some code instead?"

═══════════════════════════════════════════════
GENERAL RULES
═══════════════════════════════════════════════
- Match your answer to the QUESTION. Most messages are Mode A or B and need NO code block.
  Only Mode C uses the full explain → code → challenge flow.
- All code goes in ```python blocks with brief comments.
- Keep code short (5-20 lines) but complete and runnable.
- Use friendly analogies from everyday Indian life when useful (chai, cricket, trains, UPI).
- NEVER invent library names, functions, or APIs. If unsure, say so honestly.
- Be warm and encouraging. Address the student by name if you know it, else "coder".
- Stay strictly within Python / AI / prompt-engineering / Vibe Coding. Anything else → Mode D.

VIBE CODING PRINCIPLES (teach these when relevant):
1. Intent First — describe WHAT before writing HOW.
2. Prompt-First Development — write the AI prompt before the code.
3. Small Loops — code a little, test a little, improve a little.
4. Read Before Run — understand code before executing it.
5. Structure Over Speed — clean architecture beats quick hacks.
"""

IMAGE_ANALYSIS_PROMPT = """You are Dr. Maheshwari — an AI Professor analyzing a student's screenshot or error image.

The student has uploaded an image. It may contain:
- An error message / traceback from their code
- A screenshot of their code editor
- A screenshot of terminal output
- A diagram or flowchart they need help with

YOUR TASK:
1. 🔍 **IDENTIFY** what the image shows (error type, code snippet, output, etc.)
2. 🐛 **DIAGNOSE** the problem — explain what went wrong in simple terms.
3. 🔧 **FIX** — show the corrected code with comments explaining each change.
4. 📚 **TEACH** — explain WHY this error happens so they learn the pattern.
5. 🧪 **CHALLENGE** — give them a follow-up task to reinforce the lesson.

If you cannot read the image clearly, say so honestly and ask them to paste the error text.

RULES:
- Be specific about line numbers and exact error types when visible.
- Show the corrected code in ```python blocks.
- Keep it encouraging: "This is a very common error — even pros hit this!"
- End with 🧪 a challenge and 💡 a hint.
"""

OCR_ANALYSIS_PROMPT = """You are Dr. Maheshwari — an AI Professor at Techno NJR Institute.

A student uploaded a SCREENSHOT of their code or error. We ran OCR (text
recognition) on that screenshot and extracted the text below. OCR is not
perfect — there may be small character mistakes (e.g. 0/O, l/1/I, missing
indentation). Use your judgment to read past minor OCR noise.

YOUR TASK:
1. 🔍 IDENTIFY what the screenshot shows — an error/traceback, code, or terminal output.
2. 🐛 DIAGNOSE the actual problem in plain language.
3. 🔧 FIX it — show corrected code in a ```python block with comments on each change.
4. 📚 TEACH — explain WHY this happens so they recognise the pattern next time.
5. 🧪 CHALLENGE — a short follow-up task, then 💡 a hint.

If the extracted text is garbled or clearly incomplete, tell the student
kindly and ask them to either retake a clearer screenshot or paste the error
text directly. Be encouraging — errors are how we learn to code.

If the screenshot is clearly NOT about programming (e.g. a photo, a chat,
homework from another subject), politely say you are only trained to help
with Python and Vibe Coding, and ask them to share a coding question instead.
"""


DEEP_PROMPT_ADDITION = """
DEEP-MODE INSTRUCTIONS (apply ONLY when the question is a coding/how-to question, i.e. Mode C):
- Comprehensive code examples (15-30 lines).
- Show multiple approaches: "Here's the basic way vs the Pythonic way."
- Include type hints and docstrings in examples.
- Add a "🔬 Going Deeper" section with advanced usage.
- Challenge should be harder — design decisions or optimization.
For greetings, conceptual, or off-topic messages, follow the normal mode rules above (no forced code).
"""

# ═══════════════════════════════════════════════
# DATA MODELS
# ═══════════════════════════════════════════════

class Difficulty(str, Enum):
    BEGINNER = "beginner"
    INTERMEDIATE = "intermediate"
    ADVANCED = "advanced"

class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)
    student_id: str = Field(..., min_length=1, max_length=64)
    difficulty: Difficulty = Field(default=Difficulty.BEGINNER)
    conversation_id: Optional[str] = Field(default=None, max_length=64)

    @field_validator("message")
    @classmethod
    def _clean_message(cls, v: str) -> str:
        # Strip control characters (keep normal whitespace/newlines/tabs)
        v = "".join(ch for ch in v if ch == "\n" or ch == "\t" or ord(ch) >= 32)
        v = v.strip()
        if not v:
            raise ValueError("Message cannot be empty.")
        return v

    @field_validator("student_id", "conversation_id")
    @classmethod
    def _safe_id(cls, v: Optional[str]) -> Optional[str]:
        # IDs are server-generated tokens; restrict to a safe charset
        if v is None:
            return v
        if not re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", v):
            raise ValueError("Invalid id format.")
        return v

class RegisterRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=50)
    roll_number: str = Field(default="", max_length=20)

    @field_validator("name")
    @classmethod
    def _clean_name(cls, v: str) -> str:
        # Remove control chars and angle brackets; collapse whitespace.
        v = "".join(ch for ch in v if ord(ch) >= 32)
        v = re.sub(r"[<>]", "", v)            # no HTML markup in names
        v = re.sub(r"\s+", " ", v).strip()
        if len(v) < 2:
            raise ValueError("Please enter your full name.")
        return v[:50]

    @field_validator("roll_number")
    @classmethod
    def _clean_roll(cls, v: str) -> str:
        v = v.strip()
        if v and not re.fullmatch(r"[A-Za-z0-9/\-_. ]{0,20}", v):
            raise ValueError("Roll number has invalid characters.")
        return v

class ModelInfo(BaseModel):
    name: str
    size: str
    status: str

class HealthResponse(BaseModel):
    status: str
    ollama_connected: bool
    models: list[ModelInfo]
    active_inferences: int
    queue_depth: int
    cache_hits: int
    cache_misses: int
    cache_size: int
    uptime_seconds: float
    total_requests: int
    ram_estimate_gb: float
    connected_students: int
    vision_available: bool
    ocr_available: bool
    image_capability: str

# ═══════════════════════════════════════════════
# STUDENT REGISTRY
# ═══════════════════════════════════════════════

@dataclass
class StudentSession:
    student_id: str
    name: str
    roll_number: str
    ip_address: str
    registered_at: float
    last_active: float
    questions_asked: int = 0
    images_uploaded: int = 0
    is_online: bool = True

class StudentRegistry:
    """Track registered students and their activity."""

    ONLINE_TIMEOUT = 300  # 5 minutes without activity = offline
    MAX_STUDENTS = int(os.getenv("MAX_STUDENTS", "200"))  # hard cap to bound memory

    def __init__(self):
        self._students: dict[str, StudentSession] = {}
        self._ip_to_sid: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def register(self, name: str, roll_number: str, ip: str) -> str:
        async with self._lock:
            # Check if this IP already registered
            if ip in self._ip_to_sid:
                sid = self._ip_to_sid[ip]
                if sid in self._students:
                    # Update name if re-registering
                    self._students[sid].name = name
                    self._students[sid].roll_number = roll_number
                    self._students[sid].is_online = True
                    self._students[sid].last_active = time.time()
                    return sid

            if len(self._students) >= self.MAX_STUDENTS:
                raise HTTPException(503, "Class is full. Please ask your instructor for help.")

            sid = f"stu_{hashlib.md5(f'{name}:{ip}:{time.time()}'.encode()).hexdigest()[:8]}"
            self._students[sid] = StudentSession(
                student_id=sid,
                name=name,
                roll_number=roll_number,
                ip_address=ip,
                registered_at=time.time(),
                last_active=time.time(),
            )
            self._ip_to_sid[ip] = sid
            log.info(f"📝 Student registered: {name} ({roll_number}) → {sid}")
            return sid

    async def get(self, sid: str) -> Optional[StudentSession]:
        return self._students.get(sid)

    async def record_activity(self, sid: str, activity: str = "question"):
        async with self._lock:
            s = self._students.get(sid)
            if s:
                s.last_active = time.time()
                s.is_online = True
                if activity == "question":
                    s.questions_asked += 1
                elif activity == "image":
                    s.images_uploaded += 1

    async def update_online_status(self):
        now = time.time()
        async with self._lock:
            for s in self._students.values():
                s.is_online = (now - s.last_active) < self.ONLINE_TIMEOUT

    async def get_all(self) -> list[dict]:
        await self.update_online_status()
        result = []
        for s in self._students.values():
            result.append({
                "student_id": s.student_id,
                "name": s.name,
                "roll_number": s.roll_number,
                "ip": s.ip_address,
                "registered_at": datetime.fromtimestamp(s.registered_at).strftime("%H:%M:%S"),
                "last_active": datetime.fromtimestamp(s.last_active).strftime("%H:%M:%S"),
                "questions_asked": s.questions_asked,
                "images_uploaded": s.images_uploaded,
                "is_online": s.is_online,
            })
        # Sort: online first, then by last active
        result.sort(key=lambda x: (not x["is_online"], x["last_active"]), reverse=True)
        return result

    @property
    def online_count(self) -> int:
        now = time.time()
        return sum(1 for s in self._students.values()
                   if (now - s.last_active) < self.ONLINE_TIMEOUT)

    @property
    def total_count(self) -> int:
        return len(self._students)

# ═══════════════════════════════════════════════
# RESPONSE CACHE
# ═══════════════════════════════════════════════

@dataclass
class CacheEntry:
    response: str
    model_used: str
    timestamp: float
    hit_count: int = 0

class ResponseCache:
    def __init__(self, max_size: int = CACHE_SIZE, ttl: int = CACHE_TTL):
        self.max_size = max_size
        self.ttl = ttl
        self._cache: OrderedDict[str, CacheEntry] = OrderedDict()
        self._lock = asyncio.Lock()
        self.hits = 0
        self.misses = 0

    def _key(self, msg: str, diff: str) -> str:
        return hashlib.sha256(f"{msg.strip().lower()}:{diff}".encode()).hexdigest()[:16]

    async def get(self, msg: str, diff: str) -> Optional[CacheEntry]:
        key = self._key(msg, diff)
        async with self._lock:
            entry = self._cache.get(key)
            if entry and (time.time() - entry.timestamp) < self.ttl:
                entry.hit_count += 1
                self.hits += 1
                self._cache.move_to_end(key)
                return entry
            if entry:
                del self._cache[key]
            self.misses += 1
            return None

    async def put(self, msg: str, diff: str, response: str, model: str):
        key = self._key(msg, diff)
        async with self._lock:
            if len(self._cache) >= self.max_size and key not in self._cache:
                self._cache.popitem(last=False)
            self._cache[key] = CacheEntry(
                response=response, model_used=model, timestamp=time.time()
            )

    @property
    def size(self) -> int:
        return len(self._cache)

# ═══════════════════════════════════════════════
# RATE LIMITER
# ═══════════════════════════════════════════════

class RateLimiter:
    def __init__(self, max_req: int = RATE_LIMIT_PER_MIN, window: int = 60):
        self.max_req = max_req
        self.window = window
        self._log: dict[str, list[float]] = {}

    def check(self, ip: str) -> tuple[bool, int]:
        now = time.time()
        self._log.setdefault(ip, [])
        self._log[ip] = [t for t in self._log[ip] if now - t < self.window]
        if len(self._log[ip]) >= self.max_req:
            return False, 0
        self._log[ip].append(now)
        return True, self.max_req - len(self._log[ip])

    def cleanup(self):
        now = time.time()
        stale = [ip for ip, ts in self._log.items() if all(now - t > self.window for t in ts)]
        for ip in stale:
            del self._log[ip]

# ═══════════════════════════════════════════════
# INFERENCE QUEUE
# ═══════════════════════════════════════════════

class InferenceQueue:
    def __init__(self, max_concurrent: int = MAX_CONCURRENT):
        self._sem = asyncio.Semaphore(max_concurrent)
        self._waiting = 0
        self._active = 0
        self._total = 0
        self._lock = asyncio.Lock()

    @property
    def waiting(self): return self._waiting
    @property
    def active(self): return self._active
    @property
    def total(self): return self._total

    @asynccontextmanager
    async def acquire(self):
        async with self._lock:
            if self._waiting >= MAX_QUEUE_SIZE:
                raise HTTPException(503, "Class is full! Wait a moment.")
            self._waiting += 1
        try:
            await self._sem.acquire()
            async with self._lock:
                self._waiting -= 1
                self._active += 1
            try:
                yield
            finally:
                async with self._lock:
                    self._active -= 1
                    self._total += 1
                self._sem.release()
        except HTTPException:
            async with self._lock:
                self._waiting -= 1
            raise
        except Exception:
            async with self._lock:
                self._waiting = max(0, self._waiting - 1)
            raise

# ═══════════════════════════════════════════════
# MULTI-MODEL ROUTER
# ═══════════════════════════════════════════════

DEEP_KEYWORDS = {
    "explain in detail", "deep dive", "how does it work internally",
    "architecture", "design pattern", "system design", "compare",
    "trade-off", "pros and cons", "best practice", "optimize",
    "performance", "scalability", "under the hood", "time complexity",
    "space complexity", "step by step explain", "detailed explanation",
}

def select_model(message: str, difficulty: Difficulty) -> tuple[str, int]:
    if not MODEL_DEEP:
        return MODEL_FAST, MAX_TOKENS_FAST
    msg_lower = message.lower()
    if difficulty == Difficulty.ADVANCED or any(kw in msg_lower for kw in DEEP_KEYWORDS):
        return MODEL_DEEP, MAX_TOKENS_DEEP
    return MODEL_FAST, MAX_TOKENS_FAST

# ═══════════════════════════════════════════════
# OLLAMA CLIENT
# ═══════════════════════════════════════════════

class OllamaClient:
    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None
        self._vision_available: bool = False

    async def start(self):
        self._client = httpx.AsyncClient(
            base_url=OLLAMA_BASE_URL,
            timeout=httpx.Timeout(connect=10.0, read=None, write=30.0, pool=None),
        )

    async def stop(self):
        if self._client:
            await self._client.aclose()

    async def health(self) -> bool:
        try:
            r = await self._client.get("/api/tags")
            return r.status_code == 200
        except Exception:
            return False

    async def list_models(self) -> list[dict]:
        try:
            r = await self._client.get("/api/tags")
            return r.json().get("models", [])
        except Exception:
            return []

    async def model_status(self, model_name: str) -> ModelInfo:
        models = await self.list_models()
        for m in models:
            if model_name in m["name"] or m["name"].startswith(model_name):
                size_bytes = m.get("size", 0)
                size_gb = f"{size_bytes / (1024**3):.1f}GB" if size_bytes else "unknown"
                return ModelInfo(name=m["name"], size=size_gb, status="loaded")
        return ModelInfo(name=model_name, size="unknown", status="not_found")

    async def check_vision_model(self) -> bool:
        """Check if the vision model (llava) is available."""
        if not VISION_MODEL:
            return False
        status = await self.model_status(VISION_MODEL)
        self._vision_available = status.status == "loaded"
        return self._vision_available

    @property
    def vision_available(self) -> bool:
        return self._vision_available

    async def warmup(self, model: str):
        try:
            log.info(f"🔥 Warming up: {model}")
            await self._client.post("/api/generate", json={
                "model": model, "prompt": "hi", "stream": False,
                "options": {"num_predict": 1},
            })
            log.info(f"✅ {model} ready")
        except Exception as e:
            log.warning(f"⚠️ Warmup failed for {model}: {e}")

    async def stream(self, prompt: str, model: str, system: str,
                     max_tokens: int) -> AsyncGenerator[str, None]:
        payload = {
            "model": model, "prompt": prompt, "system": system, "stream": True,
            "options": {
                "num_predict": max_tokens, "temperature": 0.7,
                "top_p": 0.9, "top_k": 40, "repeat_penalty": 1.1,
                "num_ctx": CONTEXT_WINDOW,
            },
        }
        async with self._client.stream("POST", "/api/generate", json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line: continue
                try:
                    chunk = json.loads(line)
                    tok = chunk.get("response", "")
                    if tok: yield tok
                    if chunk.get("done"): break
                except json.JSONDecodeError:
                    continue

    async def generate(self, prompt: str, model: str, system: str,
                       max_tokens: int) -> str:
        payload = {
            "model": model, "prompt": prompt, "system": system, "stream": False,
            "options": {
                "num_predict": max_tokens, "temperature": 0.7,
                "top_p": 0.9, "top_k": 40, "repeat_penalty": 1.1,
                "num_ctx": CONTEXT_WINDOW,
            },
        }
        r = await self._client.post("/api/generate", json=payload)
        r.raise_for_status()
        return r.json().get("response", "")

    async def analyze_image(self, image_b64: str, prompt: str,
                            model: str, system: str, max_tokens: int
                            ) -> AsyncGenerator[str, None]:
        """Stream response with image input using Ollama's vision API."""
        payload = {
            "model": model,
            "prompt": prompt,
            "system": system,
            "images": [image_b64],
            "stream": True,
            "options": {
                "num_predict": max_tokens, "temperature": 0.7,
                "top_p": 0.9, "num_ctx": CONTEXT_WINDOW,
            },
        }
        async with self._client.stream("POST", "/api/generate", json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line: continue
                try:
                    chunk = json.loads(line)
                    tok = chunk.get("response", "")
                    if tok: yield tok
                    if chunk.get("done"): break
                except json.JSONDecodeError:
                    continue

# ═══════════════════════════════════════════════
# CONVERSATION STORE
# ═══════════════════════════════════════════════

class ConversationStore:
    MAX_TURNS = 6
    MAX_SESSIONS = 100

    def __init__(self):
        self._store: OrderedDict[str, list[dict]] = OrderedDict()

    def add(self, sid: str, role: str, content: str):
        if sid not in self._store:
            if len(self._store) >= self.MAX_SESSIONS:
                self._store.popitem(last=False)
            self._store[sid] = []
        self._store[sid].append({"role": role, "content": content})
        if len(self._store[sid]) > self.MAX_TURNS * 2:
            self._store[sid] = self._store[sid][-(self.MAX_TURNS * 2):]
        self._store.move_to_end(sid)

    def context(self, sid: str) -> str:
        turns = self._store.get(sid, [])
        if not turns: return ""
        lines = []
        for t in turns[-6:]:
            who = "Student" if t["role"] == "user" else "Dr. Maheshwari"
            lines.append(f"{who}: {t['content']}")
        return "\nPrevious conversation:\n" + "\n".join(lines) + "\n"

    def clear(self, sid: str):
        self._store.pop(sid, None)

# ═══════════════════════════════════════════════
# APP INITIALIZATION
# ═══════════════════════════════════════════════

ollama = OllamaClient()
cache = ResponseCache()
limiter = RateLimiter()
queue = InferenceQueue()
convos = ConversationStore()
students = StudentRegistry()
start_time = time.time()

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("=" * 56)
    log.info("  🎓 Dr. Maheshwari — AI Professor Agent v3")
    log.info("  📍 NJR i3 Labs · Techno NJR Institute, Udaipur")
    log.info("  💻 Mac Mini M4 · 16GB RAM · No Docker")
    log.info("=" * 56)

    await ollama.start()
    ok = await ollama.health()
    if ok:
        models = await ollama.list_models()
        log.info(f"✅ Ollama connected | Models: {[m['name'] for m in models]}")
        await ollama.warmup(MODEL_FAST)
        if MODEL_DEEP:
            deep = await ollama.model_status(MODEL_DEEP)
            if deep.status != "not_found":
                await ollama.warmup(MODEL_DEEP)
                log.info(f"🧠 Multi-model: {MODEL_FAST} + {MODEL_DEEP}")
            else:
                log.warning(f"⚠️  Deep model '{MODEL_DEEP}' not found. Run: ollama pull {MODEL_DEEP}")

        # Image handling capability report
        # Primary: OCR (text from screenshots). Optional: llava vision.
        if _OCR_READY:
            log.info(f"🔎 OCR ready ({_OCR_REASON}) — screenshots → text → {MODEL_FAST}")
        else:
            log.warning(f"⚠️  OCR unavailable: {_OCR_REASON}")
            log.warning(f"   For screenshot reading: brew install tesseract && pip install pytesseract pillow")

        # Check optional vision model
        has_vision = await ollama.check_vision_model()
        if has_vision:
            log.info(f"👁️  Vision model available (bonus): {VISION_MODEL}")
        elif not _OCR_READY:
            log.warning(f"⚠️  No image capability active. Students must paste error text.")
            log.warning(f"   Recommended on 16GB: enable OCR (above). Vision (llava) is heavy (~4.5GB).")
        else:
            log.info(f"ℹ️  Vision model '{VISION_MODEL}' not installed — OCR handles screenshots (recommended on 16GB).")
    else:
        log.error("❌ Cannot reach Ollama! Run: ollama serve")

    log.info(f"⚙️  Concurrency={MAX_CONCURRENT} Queue={MAX_QUEUE_SIZE} Rate={RATE_LIMIT_PER_MIN}/min")
    if TEACHER_PASSWORD == "njr2026":
        log.warning("⚠️  Teacher dashboard is using the DEFAULT password. Set TEACHER_PASSWORD in .env before class!")
    else:
        log.info("🔑 Teacher dashboard password is set (hidden).")
    log.info("🚀 Dr. Maheshwari is ready!")

    async def cleanup_loop():
        while True:
            await asyncio.sleep(120)
            limiter.cleanup()
            await students.update_online_status()

    task = asyncio.create_task(cleanup_loop())
    yield
    task.cancel()
    await ollama.stop()
    log.info("👋 Class dismissed!")


app = FastAPI(
    title="Dr. Maheshwari — AI Professor Agent v3",
    version="3.0.0",
    lifespan=lifespan,
)

# ── CORS ──
# The app serves its own frontend, so same-origin requests need no CORS.
# Cross-origin is only needed if you host the UI elsewhere. Lock this down via
# ALLOWED_ORIGINS (comma-separated) in .env; defaults to same-origin only.
_origins_env = os.getenv("ALLOWED_ORIGINS", "").strip()
_allow_origins = [o.strip() for o in _origins_env.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allow_origins,            # empty list = same-origin only
    allow_methods=["GET", "POST"],           # only what the app uses
    allow_headers=["Content-Type", "X-Teacher-Password"],
    allow_credentials=False,
)

# ── Security headers ──
# Adds standard hardening headers on every response. CSP is intentionally
# permissive for inline styles/scripts (this is a single-file self-hosted UI)
# but blocks framing, sniffing, and external object/embed loads.
@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-XSS-Protection"] = "1; mode=block"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "img-src 'self' data: blob:; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "script-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "frame-ancestors 'none'"
    )
    return resp

# ═══════════════════════════════════════════════
# ROUTES — Student Registration
# ═══════════════════════════════════════════════

@app.post("/register")
async def register_student(req: RegisterRequest, request: Request):
    ip = request.client.host if request.client else "0.0.0.0"
    # Rate-limit registration attempts per IP to prevent spam/flooding
    allowed, _ = limiter.check(ip)
    if not allowed:
        raise HTTPException(429, "Too many attempts. Please wait a minute.")
    sid = await students.register(req.name, req.roll_number, ip)
    student = await students.get(sid)
    return {
        "student_id": sid,
        "name": student.name,
        "message": f"Welcome to Vibe Coding class, {student.name}! 🎓",
        "vision_enabled": ollama.vision_available,
    }

# ═══════════════════════════════════════════════
# ROUTES — Health & Queue
# ═══════════════════════════════════════════════

@app.get("/health", response_model=HealthResponse)
async def health():
    connected = await ollama.health()
    models_info = [await ollama.model_status(MODEL_FAST)]
    if MODEL_DEEP:
        models_info.append(await ollama.model_status(MODEL_DEEP))
    if VISION_MODEL:
        models_info.append(await ollama.model_status(VISION_MODEL))

    ram = 4.3
    for m in models_info:
        if m.status == "loaded" and m.size != "unknown":
            try: ram += float(m.size.replace("GB", ""))
            except: ram += 1.5

    # Image capability summary for the frontend
    if _OCR_READY and ollama.vision_available:
        img_cap = "ocr+vision"
    elif _OCR_READY:
        img_cap = "ocr"
    elif ollama.vision_available:
        img_cap = "vision"
    else:
        img_cap = "none"

    return HealthResponse(
        status="healthy" if connected else "offline",
        ollama_connected=connected, models=models_info,
        active_inferences=queue.active, queue_depth=queue.waiting,
        cache_hits=cache.hits, cache_misses=cache.misses, cache_size=cache.size,
        uptime_seconds=round(time.time() - start_time, 1),
        total_requests=queue.total, ram_estimate_gb=round(ram, 1),
        connected_students=students.online_count,
        vision_available=ollama.vision_available,
        ocr_available=_OCR_READY,
        image_capability=img_cap,
    )

@app.get("/queue")
async def queue_status():
    return {
        "active": queue.active, "waiting": queue.waiting,
        "max_concurrent": MAX_CONCURRENT, "total_served": queue.total,
        "online_students": students.online_count,
    }

# ═══════════════════════════════════════════════
# ROUTES — Chat (Text)
# ═══════════════════════════════════════════════

@app.post("/chat")
async def chat(req: ChatRequest, request: Request):
    client_ip = request.client.host if request.client else "0.0.0.0"

    # Verify student is registered
    student = await students.get(req.student_id)
    if not student:
        raise HTTPException(401, "Please register your name first to start chatting.")

    await students.record_activity(req.student_id, "question")

    # Rate check
    allowed, remaining = limiter.check(client_ip)
    if not allowed:
        raise HTTPException(429, "Slow down, coder! Try again in a minute. 🧘")

    # Cache check
    cached = await cache.get(req.message, req.difficulty.value)
    if cached:
        log.info(f"📦 Cache hit [{student.name}]: {req.message[:40]}...")
        async def stream_cached():
            words = cached.response.split(" ")
            for i, w in enumerate(words):
                yield f"data: {json.dumps({'token': (' ' if i else '') + w})}\n\n"
                await asyncio.sleep(0.012)
            yield f"data: {json.dumps({'done': True, 'model': cached.model_used, 'cached': True})}\n\n"
        return StreamingResponse(stream_cached(), media_type="text/event-stream")

    # Select model & build prompt
    model, max_tokens = select_model(req.message, req.difficulty)
    if model == MODEL_DEEP:
        status = await ollama.model_status(MODEL_DEEP)
        if status.status == "not_found":
            model, max_tokens = MODEL_FAST, MAX_TOKENS_FAST

    context = convos.context(req.conversation_id) if req.conversation_id else ""
    difficulty_note = {
        Difficulty.BEGINNER: "\n[If this is a coding question (Mode C): keep code simple (3-10 lines), print() results, use daily-life analogies, give an easy challenge. For greetings/concepts/off-topic, follow the normal mode rules — do not force code.]\n",
        Difficulty.INTERMEDIATE: "\n[If this is a coding question (Mode C): use functions/OOP, 10-20 lines, add error handling, a moderate challenge. For greetings/concepts/off-topic, follow the normal mode rules — do not force code.]\n",
        Difficulty.ADVANCED: "\n[If this is a coding question (Mode C): decorators/generators/async, show multiple approaches, a hard challenge. For greetings/concepts/off-topic, follow the normal mode rules — do not force code.]\n",
    }
    system = SYSTEM_PROMPT + difficulty_note.get(req.difficulty, "")
    if model == MODEL_DEEP:
        system += DEEP_PROMPT_ADDITION

    prompt = f"{context}{student.name} asks: {req.message}"
    collected: list[str] = []

    async def stream_inference():
        try:
            async with queue.acquire():
                log.info(f"🎯 [{student.name}] model={model} q={queue.active}/{queue.waiting}")
                yield f"data: {json.dumps({'queue_info': {'active': queue.active, 'waiting': queue.waiting}})}\n\n"
                async for token in ollama.stream(prompt, model, system, max_tokens):
                    collected.append(token)
                    yield f"data: {json.dumps({'token': token})}\n\n"
            full = "".join(collected)
            await cache.put(req.message, req.difficulty.value, full, model)
            if req.conversation_id:
                convos.add(req.conversation_id, "user", req.message)
                convos.add(req.conversation_id, "assistant", full)
            yield f"data: {json.dumps({'done': True, 'model': model, 'cached': False})}\n\n"
        except HTTPException as e:
            yield f"data: {json.dumps({'error': e.detail})}\n\n"
        except httpx.ConnectError:
            yield f"data: {json.dumps({'error': 'Cannot reach Ollama! Ask Dr. Maheshwari to check the server.'})}\n\n"
        except Exception as e:
            log.error(f"❌ Error: {e}", exc_info=True)
            yield f"data: {json.dumps({'error': 'Something went wrong. Try again!'})}\n\n"

    return StreamingResponse(stream_inference(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Model": model})

# ═══════════════════════════════════════════════
# ROUTES — Image/Screenshot Analysis
# ═══════════════════════════════════════════════

@app.post("/chat/image")
async def chat_image(
    request: Request,
    image: UploadFile = File(...),
    message: str = Form(default="Please analyze this screenshot and help me fix the error."),
    student_id: str = Form(...),
    difficulty: str = Form(default="beginner"),
    conversation_id: str = Form(default=""),
):
    """Analyze student screenshot/error image with vision model or OCR fallback."""
    client_ip = request.client.host if request.client else "0.0.0.0"

    # Validate/sanitize form inputs (Form fields skip Pydantic, so check here)
    if not re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", student_id or ""):
        raise HTTPException(400, "Invalid student id.")
    if conversation_id and not re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", conversation_id):
        raise HTTPException(400, "Invalid conversation id.")
    if difficulty not in ("beginner", "intermediate", "advanced"):
        difficulty = "beginner"
    message = "".join(ch for ch in (message or "") if ch == "\n" or ch == "\t" or ord(ch) >= 32).strip()[:2000]
    if not message:
        message = "Please analyze this screenshot and help me fix the error."

    # Verify student
    student = await students.get(student_id)
    if not student:
        raise HTTPException(401, "Please register first.")

    await students.record_activity(student_id, "image")

    # Rate check
    allowed, _ = limiter.check(client_ip)
    if not allowed:
        raise HTTPException(429, "Slow down! Try again in a minute.")

    # Validate image type against an explicit allowlist
    allowed_types = {"image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp"}
    if not image.content_type or image.content_type.lower() not in allowed_types:
        raise HTTPException(400, "Please upload a PNG, JPG, GIF, or WEBP image.")

    contents = await image.read()
    size_mb = len(contents) / (1024 * 1024)
    if size_mb > MAX_IMAGE_SIZE_MB:
        raise HTTPException(400, f"Image too large ({size_mb:.1f}MB). Max is {MAX_IMAGE_SIZE_MB}MB.")
    if len(contents) == 0:
        raise HTTPException(400, "Empty file.")

    # Sanitize the user-supplied filename before logging/storing (strip paths & control chars)
    safe_name = os.path.basename(image.filename or "screenshot")
    safe_name = re.sub(r"[^A-Za-z0-9._\- ]", "", safe_name)[:80] or "screenshot"

    image_b64 = base64.b64encode(contents).decode("utf-8")
    log.info(f"📸 [{student.name}] Image uploaded: {safe_name} ({size_mb:.1f}MB)")

    collected: list[str] = []

    async def stream_image_analysis():
        analysis_model = MODEL_FAST
        method = "none"
        try:
            # ── TIER 1: OCR (primary, RAM-light) ──
            # Try to read text straight off the screenshot. Best for
            # error tracebacks / code / terminal output, which are text.
            ocr_text, ocr_status = ("", "unavailable")
            if _OCR_READY:
                yield f"data: {json.dumps({'token': '🔎 Reading text from your screenshot...\\n\\n'})}\n\n"
                # OCR is CPU-bound — run it off the event loop
                ocr_text, ocr_status = await asyncio.to_thread(ocr_extract_text, contents)

            async with queue.acquire():
                if ocr_status == "ok":
                    # Got readable text → hand it to the fast text model
                    method = "ocr"
                    analysis_model = MODEL_FAST
                    prompt = (
                        f"Student {student.name} uploaded a screenshot. "
                        f"They said: \"{message}\"\n\n"
                        f"--- TEXT EXTRACTED FROM THE SCREENSHOT (via OCR) ---\n"
                        f"{ocr_text}\n"
                        f"--- END OF EXTRACTED TEXT ---\n\n"
                        f"Diagnose and fix the problem shown above."
                    )
                    log.info(f"🔎 [{student.name}] OCR extracted {len(ocr_text)} chars → {analysis_model}")
                    async for token in ollama.stream(prompt, analysis_model, OCR_ANALYSIS_PROMPT, MAX_TOKENS_FAST):
                        collected.append(token)
                        yield f"data: {json.dumps({'token': token})}\n\n"

                elif ollama.vision_available:
                    # ── TIER 2: Vision model (llava) ──
                    # OCR found nothing usable (diagram, UI, handwriting) but
                    # a vision model is installed — use it.
                    method = "vision"
                    analysis_model = VISION_MODEL
                    prompt = (
                        f"{student.name} uploaded a screenshot and says: {message}\n\n"
                        f"Analyze the image. If it shows an error or code, help them fix it."
                    )
                    log.info(f"👁️ [{student.name}] OCR empty → vision model {analysis_model}")
                    yield f"data: {json.dumps({'token': '📸 Looking at your image with AI vision...\\n\\n'})}\n\n"
                    async for token in ollama.analyze_image(
                        image_b64, prompt, analysis_model, IMAGE_ANALYSIS_PROMPT, MAX_TOKENS_VISION
                    ):
                        collected.append(token)
                        yield f"data: {json.dumps({'token': token})}\n\n"

                else:
                    # ── TIER 3: Graceful text fallback ──
                    method = "fallback"
                    analysis_model = MODEL_FAST
                    if ocr_status in ("empty", "error"):
                        hint = ("I tried to read your screenshot but couldn't make out the text clearly. "
                                "Please paste the exact error text, or retake a sharper screenshot.")
                    else:
                        hint = ("I can't read images on this setup yet. "
                                "Please paste the exact error text from your terminal or editor.")
                    prompt = (
                        f"{student.name} uploaded a screenshot but the text could not be read.\n"
                        f"Student's description: {message}\n\n"
                        f"Politely tell them: {hint}\n"
                        f"Then, based on their description, give initial troubleshooting guidance "
                        f"with code examples for the most likely common errors."
                    )
                    log.info(f"📝 [{student.name}] No OCR/vision → text fallback ({ocr_status})")
                    async for token in ollama.stream(prompt, analysis_model, SYSTEM_PROMPT, MAX_TOKENS_FAST):
                        collected.append(token)
                        yield f"data: {json.dumps({'token': token})}\n\n"

            full = "".join(collected)
            if conversation_id:
                convos.add(conversation_id, "user", f"[Uploaded image: {safe_name}] {message}")
                convos.add(conversation_id, "assistant", full)

            yield f"data: {json.dumps({'done': True, 'model': analysis_model, 'method': method})}\n\n"

        except HTTPException as e:
            yield f"data: {json.dumps({'error': e.detail})}\n\n"
        except Exception as e:
            log.error(f"❌ Image analysis error: {e}", exc_info=True)
            yield f"data: {json.dumps({'error': 'Image analysis failed. Try pasting the error text instead.'})}\n\n"

    return StreamingResponse(stream_image_analysis(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


# ═══════════════════════════════════════════════
# ROUTES — Teacher Dashboard
# ═══════════════════════════════════════════════

@app.get("/teacher/api")
async def teacher_api(request: Request, password: str = ""):
    """JSON feed for teacher dashboard. Auth via X-Teacher-Password header (preferred) or query param."""
    supplied = request.headers.get("X-Teacher-Password", password)
    # Timing-safe comparison to avoid leaking the password length/content via timing
    if not hmac.compare_digest(str(supplied), str(TEACHER_PASSWORD)):
        raise HTTPException(403, "Invalid teacher password.")

    connected = await ollama.health()
    all_students = await students.get_all()

    return {
        "server": {
            "status": "healthy" if connected else "offline",
            "uptime": round(time.time() - start_time, 1),
            "active_inferences": queue.active,
            "queue_waiting": queue.waiting,
            "total_requests": queue.total,
            "cache_hits": cache.hits,
            "cache_misses": cache.misses,
            "vision_available": ollama.vision_available,
            "ocr_available": _OCR_READY,
            "image_capability": (
                "OCR + Vision" if (_OCR_READY and ollama.vision_available)
                else "OCR" if _OCR_READY
                else "Vision" if ollama.vision_available
                else "Text only"
            ),
        },
        "students": {
            "online": students.online_count,
            "total": students.total_count,
            "list": all_students,
        },
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

@app.post("/clear-history")
async def clear_history(conversation_id: str = ""):
    if not re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", conversation_id or ""):
        raise HTTPException(400, "Invalid conversation id.")
    convos.clear(conversation_id)
    return {"status": "cleared"}

# ═══════════════════════════════════════════════
# FRONTEND — Student UI
# ═══════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    base = Path(__file__).resolve().parent
    html_path = base / "frontend" / "index.html"
    if html_path.is_file():
        return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
    return HTMLResponse(f"<h1>Backend running ✅</h1><p>Place index.html in {html_path}</p>")

@app.get("/teacher", response_class=HTMLResponse)
async def serve_teacher():
    base = Path(__file__).resolve().parent
    html_path = base / "frontend" / "teacher.html"
    if html_path.is_file():
        return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Teacher dashboard not found</h1>")

# ═══════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, workers=1, log_level="info")
