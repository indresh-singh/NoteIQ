"""Build an installable Teams ZIP with a simple code-drawn N icon."""

import argparse
import json
import struct
import zlib
from pathlib import Path
from string import Template
from uuid import UUID
from zipfile import ZipFile


def icon(size: int, outline: bool) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    rows = bytearray()
    for y in range(size):
        rows.append(0)
        for x in range(size):
            nx, ny = x / size, y / size
            stroke = 0.2 < ny < 0.8 and (0.2 < nx < 0.32 or 0.68 < nx < 0.8 or abs(nx - ny) < 0.06)
            pixel = (
                (255, 255, 255, 255)
                if stroke
                else ((0, 0, 0, 0) if outline else (70, 78, 184, 255))
            )
            rows.extend(pixel)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def main() -> None:
    from app.config import settings

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--app-id", type=UUID, help="Keep an existing Teams package ID when upgrading"
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    config = settings()
    root = Path(__file__).resolve().parents[1]
    template = Template((root / "teams-app/manifest.template.json").read_text())
    from urllib.parse import urlsplit

    values = {
        "TEAMS_APP_ID": str(args.app_id or config.teams_app_id or config.graph_client_id),
        "GRAPH_CLIENT_ID": str(config.graph_client_id),
        "PUBLIC_URL": config.public_url,
        "PUBLIC_DOMAIN": urlsplit(config.public_url).hostname,
    }
    manifest = json.loads(
        template.safe_substitute({k: json.dumps(v)[1:-1] for k, v in values.items()})
    )
    output = args.output or root / "dist/noteiq-teams.zip"
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "w") as package:
        package.writestr("manifest.json", json.dumps(manifest, indent=2))
        package.writestr("color.png", icon(192, False))
        package.writestr("outline.png", icon(32, True))
    print(f"Upload this ZIP to Teams: {output}")


if __name__ == "__main__":
    main()
