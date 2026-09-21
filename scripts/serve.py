"""Run NoteIQ with uv run python -m scripts.serve."""

import os

import uvicorn

from app.config import ROOT, settings
from app.observability import configure_logging


def main():
    config = settings()
    if not (ROOT / "web/vendor/teams.min.js").exists():
        raise SystemExit(
            "Browser SDK assets are missing. Run uv run python -m scripts.vendor_assets"
        )
    configure_logging()
    host = os.getenv("NOTEIQ_HOST", "127.0.0.1")
    try:
        port = int(os.getenv("PORT", "8000"))
    except ValueError:
        raise SystemExit("PORT must be a number") from None
    if not 1 <= port <= 65535:
        raise SystemExit("PORT must be between 1 and 65535")
    print(f"NoteIQ public URL: {config.public_url}")
    print(f"NoteIQ listening on {host}:{port}")
    # No access log: OAuth callback query strings contain authorization codes.
    uvicorn.run("app.web:app", host=host, port=port, workers=1, access_log=False)


if __name__ == "__main__":
    main()
