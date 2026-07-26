"""SDK detection: heuristics first, LLM classifier only if inconclusive.

The goal of this module is to decide whether a fetched repository is an
**SDK / API client library** (as opposed to an application, framework, script,
dataset, etc.).  The result influences how aggressively we generate
documentation, but we proceed regardless of the verdict.

Detection strategy
-------------------
1.  **Heuristics** (fast, free, deterministic):
    - Repo / package name contains ``sdk`` or ``client``.
    - Python source contains classes named ``*Client``, ``*API``, ``*SDK``.
    - Directory structure has ``resources/``, ``models/``, ``endpoints/``, …
    - README mentions API keys, ``pip install``, "SDK", "client library".
    - Packaging metadata (``pyproject.toml`` / ``setup.py``) is present.

    Each signal adds to a cumulative score (0..1).

2.  **LLM fallback** (only when heuristics are inconclusive):
    - If the score is between 0.20 and 0.55 (the "grey zone"), we ask the
      LLM classifier (``llm.classify_is_sdk``) for a second opinion.
    - If the LLM is unavailable, we fall back to a mid-threshold (≥ 0.40).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .fetcher import FetchResult


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class DetectionResult:
    """Outcome of the SDK detection step."""
    is_sdk: bool
    score: float                # 0..1 — confidence score from heuristics
    reasons: list[str] = field(default_factory=list)
    method: str = "heuristic"   # "heuristic" | "llm" — how the verdict was reached

    def as_dict(self) -> dict:
        """Serialise to a plain dict for JSON API responses."""
        return {"is_sdk": self.is_sdk, "score": round(self.score, 3),
                "reasons": self.reasons, "method": self.method}


# ---------------------------------------------------------------------------
# Regex patterns for heuristic signals
# ---------------------------------------------------------------------------

# Matches class definitions like ``class FooClient:``, ``class BarAPI:``, etc.
_CLIENT_CLASS_RE = re.compile(r"^\s*class\s+([A-Z]\w*?(?:Client|API|SDK))\b", re.M)

# README mentions of API keys / auth tokens.
_APIKEY_RE = re.compile(r"api[_\- ]?key|bearer\s+token|authorization", re.I)

# README mentions of ``pip install``.
_INSTALL_RE = re.compile(r"pip\s+install\s+", re.I)

# README mentions of "SDK", "client library", "python client".
_SDK_WORDS = re.compile(r"\b(sdk|client\s+library|python\s+client)\b", re.I)

# Directory names that suggest an SDK-style package layout.
_RESOURCE_DIRS = ("resources/", "models/", "endpoints/", "services/", "operations/")


# ---------------------------------------------------------------------------
# Main detection function
# ---------------------------------------------------------------------------

def detect(result: FetchResult) -> DetectionResult:
    """Run SDK detection on a fetched repo.

    Returns a ``DetectionResult`` with a boolean verdict, a 0..1 score, a
    list of human-readable reasons, and the method used ("heuristic" or "llm").
    """
    reasons: list[str] = []
    score = 0.0

    # ------------------------------------------------------------------
    # Signal 1: Repo / package naming
    # ------------------------------------------------------------------
    # e.g. ``acme-sdk``, ``acme_client``, ``sdk-foo``
    name = result.ref.repo.lower()
    if re.search(r"(^|[-_])(sdk|client)($|[-_])", name):
        score += 0.30
        reasons.append(f"repo name '{name}' suggests SDK/client")

    # ------------------------------------------------------------------
    # Signal 2: Client-ish classes across .py files
    # ------------------------------------------------------------------
    # We scan every fetched .py file for class definitions matching
    # ``*Client``, ``*API``, or ``*SDK``.
    client_classes: list[str] = []
    for path, f in result.files.items():
        if not path.endswith(".py"):
            continue
        for m in _CLIENT_CLASS_RE.finditer(f.text):
            client_classes.append(f"{m.group(1)} ({path})")
    if client_classes:
        # More classes = higher confidence, capped at 0.35.
        score += min(0.35, 0.1 + 0.05 * len(client_classes))
        reasons.append(f"found {len(client_classes)} Client/API/SDK class(es): "
                       + ", ".join(client_classes[:3])
                       + ("..." if len(client_classes) > 3 else ""))

    # ------------------------------------------------------------------
    # Signal 3: Resource-style directory structure
    # ------------------------------------------------------------------
    # SDKs often organise code into ``resources/``, ``models/``, ``endpoints/``.
    all_paths = list(result.files.keys())
    hit_dirs = [d for d in _RESOURCE_DIRS if any(f"/{d}" in ("/" + p) for p in all_paths)]
    if hit_dirs:
        score += 0.15
        reasons.append(f"resource-style dirs present: {', '.join(hit_dirs)}")

    # ------------------------------------------------------------------
    # Signal 4: README signals
    # ------------------------------------------------------------------
    readme = _find_readme(result)
    if readme:
        text = readme.text
        if _APIKEY_RE.search(text):
            score += 0.10
            reasons.append("README references API key / auth token")
        if _INSTALL_RE.search(text):
            score += 0.05
            reasons.append("README shows `pip install` instructions")
        if _SDK_WORDS.search(text):
            score += 0.10
            reasons.append("README uses 'SDK' / 'client library' terminology")

    # ------------------------------------------------------------------
    # Signal 5: Packaging metadata
    # ------------------------------------------------------------------
    # A distributable package usually has pyproject.toml or setup.py.
    if any(p in result.files for p in ("pyproject.toml", "setup.py", "setup.cfg")):
        score += 0.05
        reasons.append("packaging metadata present")

    # Clamp score to [0, 1].
    score = min(score, 1.0)

    # ------------------------------------------------------------------
    # Decision: confident → return; grey zone → LLM fallback
    # ------------------------------------------------------------------
    if score >= 0.55:
        return DetectionResult(is_sdk=True, score=score, reasons=reasons, method="heuristic")
    if score <= 0.20:
        return DetectionResult(is_sdk=False, score=score, reasons=reasons, method="heuristic")

    # Inconclusive (0.20 < score < 0.55) → ask the LLM.
    # Imported lazily so the LLM module (and its deps) aren't loaded at
    # cold-start unless we actually need them.
    try:
        from .llm import classify_is_sdk
        verdict, rationale = classify_is_sdk(result, reasons)
        reasons.append(f"LLM classifier: {rationale}")
        return DetectionResult(is_sdk=verdict, score=score, reasons=reasons, method="llm")
    except Exception as e:  # noqa: BLE001
        # LLM unavailable — fall back to a mid-threshold and note the failure.
        reasons.append(f"LLM fallback unavailable ({e}); defaulting to score threshold")
        return DetectionResult(is_sdk=score >= 0.4, score=score, reasons=reasons, method="heuristic")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_readme(result: FetchResult):
    """Return the first fetched file whose name starts with ``readme``."""
    for path, f in result.files.items():
        base = path.rsplit("/", 1)[-1].lower()
        if base.startswith("readme"):
            return f
    return None