"""ASGI entry point: uvicorn app:app --host 0.0.0.0 --port $PORT"""

import logging

from gsc_mcp.server import build_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

app = build_app()
