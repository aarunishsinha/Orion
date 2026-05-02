# Orion Agent

A native macOS voice assistant powered by local LLM inference. Speak naturally, get intelligent responses — all running on your machine with zero cloud dependencies.

Orion listens through your microphone, transcribes speech locally via Whisper, reasons through Qwen2.5-3B via Ollama, and responds with synced text + speech through a translucent HUD overlay.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                          macOS Host                                 │
│                                                                     │
│  ┌──────────────┐    ┌──────────────────┐    ┌───────────────────┐  │
│  │  Hotkey      │    │   Orchestrator   │    │   Orion HUD       │  │
│  │  Manager     │───▶│   (Brain)        │───▶│   (PyQt6 Overlay) │  │
│  │  ⌘+⇧+J       │    │                  │    │                   │  │
│  └──────────────┘    │  ┌────────────┐  │    │  ● RECORDING      │  │
│                      │  │ Audio      │  │    │  ◐ THINKING       │  │
│  ┌──────────────┐    │  │ Engine     │  │    │  ◉ RESPONSE       │  │
│  │  Microphone  │───▶│  │ (PyAudio)  │  │    └───────────────────┘  │
│  └──────────────┘    │  └─────┬──────┘  │                           │
│                      │        │         │    ┌───────────────────┐  │
│  ┌──────────────┐    │        ▼         │    │   TTS Engine      │  │
│  │  whisper-cli │◀───│    STT (CPU)     │    │   (macOS `say`)   │  │
│  │  (Homebrew)  │───▶│                  │───▶│                   │  │
│  └──────────────┘    │  ┌────────────┐  │    │   Audio + Typing  │  │
│                      │  │ Qwen2.5-3B │  │    │   synced playback │  │
│  ┌──────────────┐    │  │ (Ollama)   │  │    └───────────────────┘  │
│  │  Ollama      │◀───│  └────────────┘  │                           │
│  │  localhost   │───▶│                  │                           │
│  └──────────────┘    └──────────────────┘                           │
│                                                                     │
│ ┌─────────────────────────────────────────────────────────────────┐ │
│ │                    Docker (docker-compose)                      │ │
│ │                                                                 │ │
│ │  ┌──────────┐   ┌────────────────┐   ┌──────────────────────┐   │ |
│ │  │  Redis   │   │  MCP Server    │   │  ARQ Worker          │   │ |
│ │  │  7-alpine│   │  (FastAPI+SSE) │   │  (Google Calendar)   │   │ |
│ │  └──────────┘   └────────────────┘   └──────────────────────┘   │ |
│ └─────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Components

### Client HUD (`client_hud/`)

The native macOS front-end — captures voice input, displays a translucent overlay, and plays TTS responses.

| Module | Purpose |
|---|---|
| `hotkey_manager.py` | Global `⌘+⇧+J` toggle listener via pynput |
| `audio_engine.py` | Mic capture (PyAudio) + Whisper STT (CPU-only, `-ng` flag) |
| `orion_hud.py` | PyQt6 translucent overlay — top-right card with typing animation |
| `tts_engine.py` | macOS native TTS via `say` + `afplay` — synced with HUD typing |

**[→ Full documentation](client_hud/README.md)**

### Orchestrator (`orchestrator/`)

The central brain — bridges hotkey events, audio capture, LLM inference, and UI state into a single pipeline.

| Property | Value |
|---|---|
| LLM | Qwen2.5-3B via Ollama (local, CPU) |
| Context | Rolling 20-message conversation history |
| TTS Sync | Audio duration → per-char typing delay calculation |
| State Lock | Redis-based busy lock (optional, graceful offline) |

**[→ Full documentation](orchestrator/README.md)**

### MCP Servers (`servers/`)

Containerized tool servers exposing functionality via the Model Context Protocol (SSE/HTTP).

| Server | Tools | Transport |
|---|---|---|
| **Google Calendar** | `list_events`, `create_event`, `update_event`, `check_conflicts` | FastMCP over SSE |

Each server runs independently in Docker with its own ARQ worker pool and Redis-backed rate limiting.

**[→ Google Calendar docs](servers/google_calendar/README.md)**

---

## Pipeline

```
User speaks → ⌘+⇧+J → Mic capture → whisper-cli (STT)
    → Qwen2.5-3B (LLM) → macOS say (TTS) + HUD typing animation
    → Audio plays + text types in sync → 1.5s pause → HUD hides
```

| Stage | Engine | Runs On |
|---|---|---|
| Speech-to-Text | `whisper-cli` (whisper.cpp 1.8.4, `ggml-base.en`) | CPU (forced via `-ng`) |
| Language Model | Qwen2.5-3B via Ollama | CPU |
| Text-to-Speech | macOS `say` (Ava Premium voice) | System |
| UI Overlay | PyQt6 frameless window | Main thread |

---

## Quick Start

### Prerequisites

```bash
# Ollama (LLM inference)
brew install ollama
brew services start ollama
ollama pull qwen2.5:3b

# Whisper (Speech-to-Text)
brew install whisper-cpp

# Python dependencies
cd client_hud && uv sync
```

### macOS Permissions

Grant these in **System Settings → Privacy & Security**:

| Permission | Why |
|---|---|
| **Accessibility** | Global hotkey capture (pynput) |
| **Microphone** | Audio input (PyAudio) |

### Run

```bash
./run.sh
```

Press `⌘+⇧+J` to start/stop recording. Press `Esc` to cancel at any time.

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `OLLAMA_URL` | `http://localhost:11434` | Ollama server address |
| `OLLAMA_MODEL` | `qwen2.5:3b` | LLM model tag |
| `OLLAMA_TIMEOUT` | `60` | Max seconds for LLM response |
| `ORION_TTS_VOICE` | `Ava (Premium)` | macOS TTS voice name |
| `ORION_TTS_RATE` | `190` | TTS speech rate (words per minute) |

---

## Project Structure

```
Orion/
├── client_hud/                 # macOS native client
│   └── src/
│       ├── hotkey_manager.py   # Global keyboard listener
│       ├── audio_engine.py     # Mic + Whisper STT
│       ├── orion_hud.py        # PyQt6 overlay HUD
│       └── tts_engine.py       # macOS native TTS
├── orchestrator/
│   └── orchestrator_v3.py      # Central pipeline controller
├── servers/
│   └── google_calendar/        # Google Calendar MCP server
│       └── src/
│           ├── mcp_server.py
│           ├── redis_worker.py
│           ├── credentials_manager.py
│           └── rate_limiter.py
├── docker-compose.yml          # Redis + MCP server + worker
├── run.sh                      # Launch script
└── README.md
```

---

## Roadmap

### 🔌 More MCP Servers
Expand Orion's capabilities with additional tool servers — email, notes, task management, smart home control, web search, and more. Each server is a standalone container exposing tools via the Model Context Protocol.

### 🧠 RAG System
Implement Retrieval-Augmented Generation to give Orion awareness of user preferences, past interactions, and personal context. A local vector database (e.g. ChromaDB) will store embeddings of user data, enabling contextually rich and personalized responses without sending data to the cloud.

### 🔗 LangGraph Framework
Integrate LangGraph to build stateful, multi-step reasoning workflows. This will enable Orion to learn from interactions over time, adapt its behavior to user patterns, and deliver an increasingly personalized experience — evolving from a generic assistant into one that truly understands its user.

### ⚡ Performance Improvements
Optimize end-to-end latency across the pipeline — faster STT with distilled Whisper models, streaming LLM inference for first-token latency reduction, and pre-generated TTS caching for common responses. Target: sub-3-second voice-to-voice round-trip.
