"""Entry point for the local API server (also used as the Tauri sidecar target)."""
from __future__ import annotations

import uvicorn

from app.config import API_HOST, API_PORT
from app.db import init_db


def main() -> None:
    init_db()
    uvicorn.run("app.api:app", host=API_HOST, port=API_PORT, reload=False)


if __name__ == "__main__":
    main()
