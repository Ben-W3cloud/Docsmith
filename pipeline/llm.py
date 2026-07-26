"""Thin OpenAI-compatible LLM client wrapper.

This module is the *only* place in the codebase that talks to a language model.
It exposes two higher-level helpers used by the pipeline:

    classify_is_sdk()  — used by `detector.py` as a fallback classifier.
    write_section()    — used by `generator.py` to author markdown doc sections.

---------------------------------------------------------------------------
Configuration (environment variables)
---------------------------------------------------------------------------
The client reads three variables, all of which are defined in ``.env``:

    BASE_URL   — OpenAI-compatible endpoint base URL.
    API_KEY    — Bearer token / API key sent in the Authorization header.
    MODEL      — The model identifier to use for completions.

These intentionally match the names in ``.env`` so there is a single source of
truth.  ``python-dotenv`` is used to load ``.env`` automatically in local dev;
in production (Vercel) the variables are injected via the platform's env system.

---------------------------------------------------------------------------
Design notes
---------------------------------------------------------------------------
* The OpenAI client is created **lazily** (singleton) so that simply importing
  this module never triggers network I/O or requires credentials.  This keeps
  Flask cold-starts cheap and avoids crashes when env vars are absent.
* Only the OpenAI Chat Completions API is supported.  Anthropic and other
  providers have been intentionally removed to keep the surface area small and
  the configuration simple.
* System prompts are crafted to produce structured, deterministic-ish output
  and to minimise hallucination (the model is told to never invent APIs).
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Optional

# ---------------------------------------------------------------------------
# dotenv — load variables from a local .env file if present.
# In production this is a no-op (the file won't exist / vars come from the
# platform).  We import defensively so the app still boots if the package is
# somehow missing.
# ---------------------------------------------------------------------------
try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # noqa: BLE001
    # dotenv is optional at runtime; env vars may already be set by the host.
    pass

from openai import OpenAI


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------
def _env(name: str, *, required: bool = False) -> Optional[str]:
    """Read an environment variable, stripping surrounding quotes.

    ``.env`` values are often written as ``KEY="value"``; ``os.environ`` on
    some platforms keeps the quotes, so we normalise them here.
    """
    val = os.environ.get(name)
    if val is not None:
        val = val.strip().strip('"').strip("'")
    if required and not val:
        raise RuntimeError(f"{name} is not set. Please define it in .env or your environment.")
    return val


def _base_url() -> str:
    """OpenAI-compatible base URL (e.g. ``https://api.openai.com/v1``)."""
    return _env("BASE_URL", required=True)  # type: ignore[return-value]


def _api_key() -> str:
    """API key / bearer token for the LLM endpoint."""
    return _env("API_KEY", required=True)  # type: ignore[return-value]


def _model() -> str:
    """Model identifier to use for completions (e.g. ``gpt-4o``)."""
    return _env("MODEL", required=True)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Lazy singleton client
# ---------------------------------------------------------------------------
# We cache the client so we don't reconstruct it on every call.  ``None`` means
# "not yet created"; the first call to ``_get_client()`` builds it.
_client: Optional[OpenAI] = None


def _get_client() -> OpenAI:
    """Return a process-wide singleton ``OpenAI`` client.

    Created on first use (not at import time) so that importing this module
    is side-effect-free and never fails due to missing credentials.
    """
    global _client
    if _client is None:
        _client = OpenAI(
            base_url=_base_url(),
            api_key=_api_key(),
        )
    return _client


# ---------------------------------------------------------------------------
# Low-level completion helper
# ---------------------------------------------------------------------------
def complete(
    system: str,
    user: str,
    *,
    max_tokens: int = 2048,
    temperature: float = 0.2,
) -> str:
    """Send a single chat-completion request and return the text response.

    Parameters
    ----------
    system
        The system prompt — sets role, tone, and output constraints.
    user
        The user message — typically a JSON-serialised payload.
    max_tokens
        Cap on generated tokens.  Keep small for classification, larger for
        prose generation.
    temperature
        Lower = more deterministic.  Use 0.0 for classification, 0.2–0.4
        for documentation prose.

    Returns
    -------
    str
        The assistant's message content, stripped of surrounding whitespace.

    Raises
    ------
    RuntimeError
        If required env vars are missing.
    openai.OpenAIError
        If the API request fails (network, auth, rate-limit, etc.).
    """
    client = _get_client()
    resp = client.chat.completions.create(
        model=_model(),
        max_tokens=max_tokens,
        temperature=temperature,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    # ``content`` can be ``None`` if the model returns a function-call-only
    # response; guard with ``or ""``.
    return (resp.choices[0].message.content or "").strip()


# ---------------------------------------------------------------------------
# Higher-level helper: SDK classification (LLM fallback for detector.py)
# ---------------------------------------------------------------------------
def classify_is_sdk(result, reasons: list[str]) -> tuple[bool, str]:
    """LLM fallback for ``detector.detect()``.

    Called only when heuristics are inconclusive (score between 0.20 and
    0.55).  Sends a compact, privacy-safe summary of the repo surface (file
    paths + README head) — **never raw source code** — and asks the model to
    classify whether the repo is an SDK / API client library.

    Parameters
    ----------
    result
        A ``fetcher.FetchResult`` with the fetched file tree + contents.
    reasons
        Heuristic reasons already computed by ``detector.detect()``; included
        as context so the LLM can build on them.

    Returns
    -------
    (bool, str)
        ``(is_sdk, rationale)`` — ``rationale`` is capped at 400 chars.
    """
    # Local import to avoid a circular import at module load time.
    from .fetcher import FetchResult  # noqa: F401  (type hint only)

    # --- Build a compact "surface" view of the repo -----------------------
    # We send at most 80 file paths — enough signal, cheap on tokens.
    surface: list[str] = list(result.files.keys())[:80]

    # Grab the first README we find and send its head (≤ 2500 chars).
    readme_snippet = ""
    for path, f in result.files.items():
        if path.rsplit("/", 1)[-1].lower().startswith("readme"):
            readme_snippet = f.text[:2500]
            break

    # --- System prompt -----------------------------------------------------
    # Carefully scoped: strict JSON output, clear definition of "SDK", and
    # an explicit instruction to *not* include raw source in the rationale.
    system = (
        "You are a precise repository classifier.\n"
        "\n"
        "Your task: decide whether a GitHub repository is an **SDK / API "
        "client library** (a library whose primary purpose is to let users "
        "call an external API or service) as opposed to an application, "
        "framework, script, dataset, or general-purpose package.\n"
        "\n"
        "Respond STRICTLY as minified JSON with exactly two keys:\n"
        '  {"is_sdk": <boolean>, "rationale": "<one or two sentences>"}\n'
        "\n"
        "Rules:\n"
        "1. Output ONLY the JSON object — no markdown fences, no preamble, "
        "no trailing text.\n"
        "2. \"rationale\" must be concise (max 40 words) and must NOT quote "
        "or paraphrase raw source code.\n"
        "3. If there is insufficient evidence, lean towards false.\n"
        "4. A repo that merely *uses* an SDK is not itself an SDK."
    )

    # --- User payload (JSON-serialised) -----------------------------------
    user = json.dumps({
        "repo": f"{result.ref.owner}/{result.ref.repo}",
        "heuristic_reasons": reasons,
        "file_paths_sample": surface,
        "readme_head": readme_snippet,
    })

    # --- Call the model ----------------------------------------------------
    raw = complete(system, user, max_tokens=400, temperature=0.0)

    # --- Parse the JSON response defensively -------------------------------
    # The model should return pure JSON, but we tolerate surrounding text by
    # extracting the first ``{...}`` block we find.
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return (False, f"unparseable LLM output: {raw[:200]}")
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return (False, f"invalid JSON from LLM: {raw[:200]}")

    return bool(data.get("is_sdk")), str(data.get("rationale", ""))[:400]


# ---------------------------------------------------------------------------
# Higher-level helper: documentation section authoring (used by generator.py)
# ---------------------------------------------------------------------------
def write_section(section: str, ir_slice: dict, context: dict, extra: str = "") -> str:
    """Produce GitHub-flavored Markdown for a single documentation section.

    Only the intermediate representation (IR) + curated context is sent to
    the model — **raw source code is never transmitted**.  This keeps prompts
    bounded and avoids leaking proprietary implementation details.

    Parameters
    ----------
    section
        Human-readable section title (e.g. ``"Getting Started"``).
    ir_slice
        Compact IR dict produced by ``generator._ir_slice_for_section()``.
    context
        Curated context dict (README head, changelog, examples) from
        ``analyzer.gather_context()``.
    extra
        Section-specific instructions from ``generator.LLM_SECTIONS``.

    Returns
    -------
    str
        Markdown content for the section (no leading H1 — that's added by
        ``generator._ensure_h1()``).
    """
    # --- System prompt -----------------------------------------------------
    # Structured, opinionated, and anti-hallucination.  The model is told
    # exactly what format to produce and explicitly forbidden from inventing
    # APIs that don't exist in the IR.
    system = (
        "You are a senior technical writer producing documentation for a "
        "Python SDK.\n"
        "\n"
        "Output rules (follow exactly):\n"
        "1. Output GitHub-flavored Markdown ONLY — no HTML, no preamble, no "
        "trailing commentary, no explanations of what you are doing.\n"
        "2. Use fenced ```python code blocks for all code examples.\n"
        "3. Use MkDocs Material admonitions (`!!! note`, `!!! warning`, "
        "`!!! tip`) sparingly and only when genuinely helpful.\n"
        "4. Keep all code examples runnable and idiomatic.\n"
        "5. NEVER invent APIs, classes, methods, parameters, or behaviours "
        "that are not present in the provided IR. If information is missing, "
        "omit it rather than guess.\n"
        "6. Use H2 (`##`) for sub-sections within the document. Do NOT emit "
        "an H1 — the system adds one for you.\n"
        "7. When referencing code identifiers, wrap them in backticks.\n"
        "8. Match the tone of the README if one is provided in the context.\n"
        "9. Be concise and practical. Prefer showing over telling.\n"
        "10. If the section is not applicable to this SDK (e.g. framework "
        "integrations for a framework-agnostic library), say so briefly "
        "instead of padding with irrelevant content."
    )

    # --- User payload ------------------------------------------------------
    # We cap the serialised payload at 120k chars to stay within most model
    # context windows while leaving room for the response.
    user_payload = {
        "section": section,
        "instructions": extra,
        "ir": ir_slice,
        "context": context,
    }

    return complete(
        system,
        json.dumps(user_payload)[:120_000],
        max_tokens=3000,
        temperature=0.3,
    )