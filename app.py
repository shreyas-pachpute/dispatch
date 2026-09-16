"""Vercel entrypoint: the FastAPI app lives in apps/api/dispatch."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "apps" / "api"))

from dispatch.main import app  # noqa: E402,F401
