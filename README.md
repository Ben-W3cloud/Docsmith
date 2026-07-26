# SDK Auto-Doc Generator

> Paste a GitHub URL. Get a full MkDocs documentation site — no clone, no config.

A Flask app that takes a GitHub repo URL, detects whether it's an SDK, and
generates a complete, navigable MkDocs (Material theme) documentation site.
The repo is never cloned; everything is fetched via the GitHub REST API.

---------------------------------------------------------------------------
Tech stack
---------------------------------------------------------------------------
| Layer | Technology |
|-------|-----------|
| Web server | Flask 3.0 (Python) |
| Frontend | Vanilla JS + CSS (no build step) |
| LLM | OpenAI-compatible API (OpenAI, NVIDIA, Groq, etc.) |
| Static site | MkDocs 1.6 + Material theme 9.5 |
| Deployment | Vercel serverless (Python runtime) |
| Auth | GitHub PAT (optional, raises rate limit) |

---------------------------------------------------------------------------
Architecture
---------------------------------------------------------------------------
```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   Browser   │────▶│  Flask API  │────▶│   jobs.py   │
│  (index.html)│     │ (index.py)  │     │ (registry + │
└─────────────┘     └─────────────┘     │  background  │
      ▲                                    │   thread)   │
      │                                    └──────┬──────┘
      │                                             │
      │  poll /api/status/<id>                      │
      │◀────────────────────────────────────────────┘
      │
      │  download /api/download/<id>
      │◀────────────────────────────────────────────┐
      │                                             │
      │  ┌──────────────────────────────────────────▼──────────┐
      │  │                    Pipeline                          │
      │  │                                                     │
      │  │  fetcher.py  →  detector.py  →  analyzer.py         │
      │  │       ↓              ↓                ↓              │
      │  │  fetch repo    SDK detect     AST → IR              │
      │  │       ↓              ↓                ↓              │
      │  │  filter tree   heuristics     gather context         │
      │  │       ↓              ↓                ↓              │
      │  │  raw files     LLM fallback   README, examples       │
      │  │       └──────────────┬──────────────────┘           │
      │  │                      ↓                              │
      │  │              generator.py                           │
      │  │           LLM-authored sections                     │
      │  │           + deterministic API ref                   │
      │  │                      ↓                              │
      │  │              builder.py                             │
      │  │           mkdocs.yml + build + zip                  │
      │  └─────────────────────────────────────────────────────┘
      │
      ▼
┌─────────────┐
│  docs.zip   │
└─────────────┘
```

---------------------------------------------------------------------------
Pipeline (step by step)
---------------------------------------------------------------------------
All data is fetched via the GitHub REST API — the repo is **never cloned**.

1. **Fetch** — Get the recursive file tree (`/git/trees/{branch}?recursive=1`),
   then selectively download file contents from `raw.githubusercontent.com`.
   Filtering keeps only `.py`, README, CHANGELOG, packaging metadata, and
   `examples/` files.  Capped at 400 files by default.

2. **Detect** — Heuristic scoring (0..1) based on:
   - Repo/package name (`*sdk*`, `*client*`)
   - Classes named `*Client`, `*API`, `*SDK`
   - Directory structure (`resources/`, `models/`, `endpoints/`)
   - README signals (API keys, `pip install`, "SDK" terminology)
   - Packaging metadata (`pyproject.toml`, `setup.py`)

   If the score is in the grey zone (0.20–0.55), an LLM classifier is used
   as a fallback.

3. **Analyze** — Python's `ast` module extracts an **intermediate
   representation (IR)**: classes, methods, signatures, type hints, docstrings,
   imports.  Raw source is **never** sent to the LLM.

4. **Context** — Curate README head, CHANGELOG head, and up to 6 example
   files for the LLM to reference.

5. **Generate** — One LLM call per documentation section (Getting Started,
   Guides, Framework Integrations, Advanced Usage, Troubleshooting).  The API
   Reference is rendered deterministically from the IR (guaranteed accurate).

6. **Build** — Write markdown + `mkdocs.yml`, run `mkdocs build` with the
   Material theme, zip the `site/` directory.

---------------------------------------------------------------------------
Environment variables
---------------------------------------------------------------------------
| Variable | Required | Purpose |
|----------|----------|---------|
| `BASE_URL` | Yes | OpenAI-compatible endpoint base URL |
| `API_KEY` | Yes | Bearer token / API key for the LLM |
| `MODEL` | Yes | Model identifier (e.g. `gpt-4o`, `deepseek-ai/deepseek-v4-pro`) |
| `GITHUB_TOKEN` | No | GitHub PAT — raises API rate limit from 60 → 5,000/hr |

All four are defined in `.env` for local development.  On Vercel, they are
injected via the platform's environment variable system.

---------------------------------------------------------------------------
Local development
---------------------------------------------------------------------------
```bash
# 1. Clone and enter
git clone <your-repo-url>
cd sdk-autodoc

# 2. Create virtual environment
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS / Linux:
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
cp .env.example .env
# Edit .env with your API keys and model settings

# 5. Run the dev server
python api/index.py
# Or with Flask:
# export FLASK_APP=api/index.py
# flask run --port 5000

# 6. Open http://localhost:5000
```

---------------------------------------------------------------------------
Deploy to Vercel
---------------------------------------------------------------------------
```bash
# 1. Link the project
vercel link

# 2. Set environment variables (or use Vercel dashboard)
vercel env add BASE_URL
vercel env add API_KEY
vercel env add MODEL
vercel env add GITHUB_TOKEN   # optional but recommended

# 3. Deploy
vercel --prod
```

**Important Vercel constraints:**
- **Execution time**: 10s on Hobby, 60s on Pro. The pipeline runs in a
  background thread; the client polls for status. Very large repos may exceed
  this limit — consider a proper queue (Vercel Queues, Inngest) for production.
- **Ephemeral filesystem**: `/tmp` is writable but cleared between invocations.
  Artifacts are returned as a zip in the same request.
- **Cold starts**: `mkdocs build` adds latency. The in-memory job registry
  does not survive cold starts — for production, use Redis / Vercel KV.

---------------------------------------------------------------------------
Project layout
---------------------------------------------------------------------------
```
.
├── api/
│   └── index.py              Flask app (entry point for Vercel)
├── pipeline/
│   ├── __init__.py           Package marker
│   ├── fetcher.py            GitHub tree + raw file fetch (no clone)
│   ├── detector.py           SDK heuristics + optional LLM classifier
│   ├── analyzer.py           AST → IR extraction
│   ├── generator.py          Per-section LLM doc generation + API-ref templating
│   ├── builder.py            MkDocs assembly + build + zip
│   ├── jobs.py               In-memory job registry with background execution
│   └── llm.py                Thin OpenAI-compatible LLM client wrapper
├── templates/
│   └── index.html            Single-page frontend (vanilla JS)
├── static/
│   └── app.css               Frontend styles (dark theme, no dependencies)
├── .env                      Your secrets (git-ignored)
├── .env.example              Template for .env
├── .gitignore                Git ignore rules
├── requirements.txt          Python dependencies
├── vercel.json               Vercel deployment config
└── README.md                 This file
```

---------------------------------------------------------------------------
API routes
---------------------------------------------------------------------------
| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/` | Serve the frontend form |
| `POST` | `/api/generate` | Submit a repo URL → returns `job_id` |
| `GET` | `/api/status/<job_id>` | Poll job status + progress |
| `GET` | `/api/download/<job_id>` | Download the generated docs zip |

---------------------------------------------------------------------------
Security notes
---------------------------------------------------------------------------
- **Never commit `.env`** — it contains real API keys. Use `.env.example` as
  a template. The `.gitignore` excludes `.env` by default.
- **GitHub tokens** should have the minimum required scope (`public_repo` for
  reading public repos). Fine-grained PATs with no permissions also work for
  public repo reads.
- **LLM API keys** are sent directly to your chosen provider (OpenAI, NVIDIA,
  etc.). They never leave your control.
- **XSS protection** — the frontend escapes all user-controlled strings before
  rendering them into HTML.

---------------------------------------------------------------------------
Limitations & roadmap
---------------------------------------------------------------------------
- In-memory job registry (lost on cold start / restart).
- No persistent artifact storage (zip is ephemeral).
- Background threads may be killed on serverless timeout.
- Single provider (OpenAI-compatible) — Anthropic removed in this version.
- No rate limiting on the `/api/generate` endpoint.
- MkDocs build may fail in restricted serverless environments (falls back to
  raw markdown zip).

Future improvements:
- Redis / Vercel KV for job state persistence.
- Vercel Blob / S3 for artifact storage.
- Proper queue (Vercel Queues, Inngest) for large repos.
- Rate limiting + request validation middleware.
- Support for multiple output formats (PDF, single-page HTML).