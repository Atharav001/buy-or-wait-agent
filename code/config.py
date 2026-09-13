"""Environment-driven configuration (no secrets in the repo).

Reads API keys from BOT_BASE environment variables / .env. Provider selection
per DEC-027: OPENAI_API_KEY or GEMINI_API_KEY, model names overridable.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env if present (repo root or code/). Keys live only in env, never git.
_here = Path(__file__).resolve().parent
load_dotenv(_here.parent / ".env")
load_dotenv(_here / ".env")

# --- pipeline constants (design.md / PRD) -----------------------------------
FORECAST_HORIZON_DAYS = 90
MAX_SPENDING_CHANGES = 3
CACHE_DIR = Path(os.environ.get("EXTRACTION_CACHE_DIR", _here / ".extraction_cache"))

# --- provider selection (DEC-027) --------------------------------------------
# EXTRACTION_PROVIDER explicit override, else auto-detect from present key.
_PROVIDER = os.environ.get("EXTRACTION_PROVIDER", "").strip().lower()


def _default_provider() -> str | None:
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    if os.environ.get("GEMINI_API_KEY"):
        return "gemini"
    return None


EXTRACTION_PROVIDER: str | None = _PROVIDER or _default_provider()
MODEL_OPENAI = os.environ.get("MODEL_OPENAI", "gpt-4o-mini")
MODEL_GEMINI = os.environ.get("MODEL_GEMINI", "gemini-2.0-flash")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
VISION_MODEL = os.environ.get("MODEL_VISION", MODEL_OPENAI if EXTRACTION_PROVIDER == "openai" else MODEL_GEMINI)

# path to the media/images directory, configurable for tests
MEDIA_IMAGES_DIR = Path(
    os.environ.get("MEDIA_IMAGES_DIR", _here.parent / "dataset" / "media" / "images")
)