<div align="center">

# 🎓 Dr. Maheshwari — AI Professor Agent

### A private, offline AI teaching assistant for the classroom

*Teaches Python & "Vibe Coding" (AI-assisted development) to 50 students at once — running entirely on a single Mac Mini. No cloud. No data leaving the room.*

![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-async-009688?logo=fastapi&logoColor=white)
![Ollama](https://img.shields.io/badge/Ollama-local%20LLM-000000?logo=ollama&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-ready-2496ED?logo=docker&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green.svg)

</div>

---

## 📖 Overview

**Dr. Maheshwari** is a self-hosted AI professor that runs on local hardware and teaches programming the Socratic way — explaining, demonstrating, and challenging students rather than just dumping answers. It was built to give an entire classroom access to an always-available coding mentor without sending a single keystroke to the cloud.

The whole system runs on a **Mac Mini M4 (16 GB RAM)** using **Ollama + `gemma2:2b` + FastAPI** — deliberately built *without* Docker or LangChain in its native form to squeeze maximum performance out of limited RAM, while still shipping an optional containerized build for portable deployment.

---

## ✨ Features

- 🧠 **Context-aware teaching** — reads each question and responds in the right mode: a warm reply to a greeting, a clear explanation for a concept, the full *explain → code → challenge → hint* flow for a coding problem, and a polite "I'm not trained on that" for off-topic questions.
- 🔎 **Screenshot & error reading** — students can drop a screenshot of an error or their code. The agent extracts the text via **Tesseract OCR** (≈50 MB RAM, sub-second) and diagnoses it — with an optional heavier vision model for diagrams.
- 👤 **Name-first registration** — every student enters their name before chatting, so the instructor always knows who's in the room.
- 📊 **Live teacher dashboard** — see who's online, questions asked, screenshots uploaded, queue depth, and server health, refreshing every few seconds.
- ⚡ **Built for 50 concurrent students** — async FastAPI backend, request queue with concurrency limits, response caching, and per-IP rate limiting.
- 🔒 **Security-hardened** — XSS-safe rendering, server-side input sanitization, constant-time teacher auth, security headers, and abuse limits.
- 🎨 **Clean white UI** — a polished, distraction-free interface with the instructor's own avatar.
- 🐳 **Optional Docker build** — bundles Ollama, the model, Tesseract, and the app into one image for Linux/server deployment.

---

## 🏗️ Tech Stack

| Layer | Technology |
|---|---|
| LLM runtime | [Ollama](https://ollama.com) (`gemma2:2b`, optional `qwen2.5:3b` / `llava:7b`) |
| Backend | FastAPI · Uvicorn · httpx (direct async calls — no LangChain) |
| OCR | Tesseract via `pytesseract` + Pillow |
| Frontend | Single-file HTML/CSS/JS (no build step) |
| Hardware | Mac Mini M4 · 16 GB RAM |

**Deliberate architecture choices:** no Docker in the native path (~500 MB RAM saved), no LangChain (~200 MB saved), and a hard ceiling on model size to avoid swap — all so a 16 GB machine can serve a full classroom.

---

## 🚀 Quick Start

### Option A — Native (recommended on Mac, fastest)

```bash
# 1. Install Ollama and the teaching model
ollama pull gemma2:2b

# 2. Install Tesseract (for screenshot reading)
brew install tesseract

# 3. Launch — creates the venv and installs deps automatically
chmod +x start.sh
./start.sh
```

### Option B — Docker (portable, best on a Linux/GPU server)

```bash
docker build -t dr-maheshwari .
docker run -p 8000:8000 -e TEACHER_PASSWORD=your-password dr-maheshwari
```

> ⚠️ **On Apple Silicon, Ollama in Docker is CPU-only** (no Metal GPU passthrough), so the native option above is faster on a Mac. Use the image on a Linux host for production.

### Access

| Who | URL |
|---|---|
| 👨‍🎓 Students | `http://<host-ip>:8000` |
| 👩‍🏫 Teacher | `http://<host-ip>:8000/teacher` |

Find the host IP with `ipconfig getifaddr en0` (macOS).

---

## ⚙️ Configuration

All settings live in `.env`:

| Variable | Default | Description |
|---|---|---|
| `MODEL_FAST` | `gemma2:2b` | The teaching model |
| `MODEL_DEEP` | *(blank)* | Optional heavier model for deep dives |
| `VISION_MODEL` | *(blank)* | Optional vision model (e.g. `llava:7b`) |
| `OCR_ENABLED` | `true` | Screenshot text reading |
| `MAX_CONCURRENT` | `3` | Simultaneous model calls |
| `MAX_TOKENS_FAST` | `1500` | Room for the full teaching answer |
| `CONTEXT_WINDOW` | `4096` | Prompt + history + reply |
| `TEACHER_PASSWORD` | `njr2026` | **Change before class!** |
| `ALLOWED_ORIGINS` | *(blank)* | CORS — same-origin only by default |
| `MAX_STUDENTS` | `200` | Memory-bounding cap |

---

## 🔌 API Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/` | GET | Student UI |
| `/register` | POST | Register a student name |
| `/chat` | POST | Text chat (SSE streaming) |
| `/chat/image` | POST | Screenshot upload + analysis |
| `/health` | GET | System health & model status |
| `/teacher` | GET | Teacher dashboard |
| `/teacher/api` | GET | Dashboard JSON feed (auth required) |

---

## 🔒 Security

This is a self-hosted LAN tool with **no database** (all state is in-memory), so SQL injection does not apply. The hardening that *does*:

- **XSS protection** — all chat content is HTML-escaped before rendering; student names are escaped on the dashboard; markdown still renders.
- **Input validation** — names/messages stripped of control characters and markup server-side; IDs constrained to a safe charset; uploads checked for type, size, emptiness, and decompression-bomb dimensions; filenames sanitized.
- **Teacher auth** — password sent via the `X-Teacher-Password` header (not the URL), compared in constant time; the server warns if the default is still in use.
- **Headers & CORS** — CSP, `X-Frame-Options: DENY`, `nosniff` on every response; CORS same-origin by default.
- **Abuse limits** — per-IP rate limiting on chat, image, and registration; `MAX_STUDENTS` cap.

> **Before a live class:** set a real `TEACHER_PASSWORD` and keep the host on the classroom LAN only — not exposed to the public internet.

---

## 📁 Project Structure

```
ai-professor-agent/
├── main.py                 # FastAPI backend (chat, OCR, dashboard, security)
├── frontend/
│   ├── index.html          # Student UI
│   └── teacher.html        # Teacher dashboard
├── start.sh                # One-click native launcher
├── requirements.txt        # Python dependencies
├── .env                    # Configuration
├── Dockerfile              # All-in-one container (Ollama + model + app)
├── docker-entrypoint.sh    # Container startup
├── docker-compose.yml      # Compose deployment
└── README.md
```

---

## 👨‍💻 Author

**Developed by Dr. Aaditya Maheshwari**
Head of Innovation & Incubation — **NJR i3 Labs Pvt. Ltd.** & **Techno NJR Institute of Technology**, Udaipur, Rajasthan, India.

*PhD in Health Informatics & Machine Learning · M.Tech (AI/ML), BITS Pilani*


- 🔗 LinkedIn: `[https://linkedin.com/in/your-handle](https://www.linkedin.com/in/aadityamah/)`


> Built at **NJR i3 Labs** to bring private, on-device AI tutoring into the classroom.

---

## 📜 License

Released under the **MIT License** — see [`LICENSE`](LICENSE) for details.
You are free to use, modify, and share it; attribution to the author is appreciated.

---

## 🙏 Acknowledgements

- [Ollama](https://ollama.com) for the local LLM runtime
- [Google's Gemma](https://ai.google.dev/gemma) models
- [Tesseract OCR](https://github.com/tesseract-ocr/tesseract)
- The students of **Techno NJR Institute**, who make tools like this worth building.

---

<div align="center">

*Made with ☕ and Python in Udaipur · © Dr. Aaditya Maheshwari*

</div>
