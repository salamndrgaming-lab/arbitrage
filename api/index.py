"""Vercel serverless entrypoint — exposes the ASGI app."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arb.webapp import app  # noqa: E402,F401
