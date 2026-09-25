"""Build an installable Teams ZIP with a simple code-drawn app icon."""

import argparse
import json
import struct
import zlib
from pathlib import Path
from string import Template
from uuid import UUID
from zipfile import ZipFile

GLYPHS = {
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "N": ("10001", "11001", "11001", "10101", "10011", "10011", "10001"),
}


def icon(size: int, outline: bool, text: str = "N") -> bytes:
    """Draw one or two white block letters on a Teams-compatible PNG."""

    letters = text.strip().upper()
    if not 1 <= len(letters) <= 2 or any(letter not in GLYPHS for letter in letters):
        raise ValueError("Icon text must contain one or two supported letters: D or N")

    gap = 1
    glyph_width = 5
    design_width = len(letters) * glyph_width + (len(letters) - 1) * gap
    scale = max(1, min(size // 10, (size * 3 // 4) // design_width))
    design_height = 7 * scale
    rendered_width = design_width * scale
    left = (size - rendered_width) // 2
    top = (size - design_height) // 2

    def is_letter_pixel(x: int, y: int) -> bool:
        column = (x - left) // scale
        row = (y - top) // scale
        if not (left <= x < left + rendered_width and top <= y < top + design_height):
            return False
        letter_stride = glyph_width + gap
        letter_index, letter_column = divmod(column, letter_stride)
        return (
            letter_index < len(letters)
            and letter_column < glyph_width
            and GLYPHS[letters[letter_index]][row][letter_column] == "1"
        )

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    rows = bytearray()
    for y in range(size):
        rows.append(0)
        for x in range(size):
            pixel = (
                (255, 255, 255, 255)
                if is_letter_pixel(x, y)
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
    parser.add_argument("--app-name", default="NoteIQ", help="User-facing Teams app name")
    parser.add_argument("--icon-text", default="N", help="One or two letters drawn in the icon")
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
        "APP_NAME": args.app_name,
    }
    manifest = json.loads(
        template.safe_substitute({k: json.dumps(v)[1:-1] for k, v in values.items()})
    )
    output = args.output or root / "dist/noteiq-teams.zip"
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "w") as package:
        package.writestr("manifest.json", json.dumps(manifest, indent=2))
        package.writestr("color.png", icon(192, False, args.icon_text))
        package.writestr("outline.png", icon(32, True, args.icon_text))
    print(f"Upload this ZIP to Teams: {output}")


if __name__ == "__main__":
    main()
