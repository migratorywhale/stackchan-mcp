#!/usr/bin/env python3
"""Generate firmware/src/gif_assets.h from named GIF assets.

The script is intentionally conservative:
- it requires the full expected expression set to be present;
- it rejects duplicate or unknown expression files;
- it refuses to write an empty header when no GIF inputs exist.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile

EXPECTED_EXPRESSIONS = (
    "calm",
    "thinking",
    "happy",
    "sleepy",
    "shy",
    "smug",
    "pouty",
)
HEADER_PREAMBLE = """#pragma once
#include <Arduino.h>

"""
FILENAME_PATTERN = re.compile(r"^(?P<prefix>[^_]+)_(?P<expression>[a-z0-9]+)\.gif$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate firmware/src/gif_assets.h from GIF files in firmware/data/. "
            "Expected filenames follow the pattern A_calm.gif, B_thinking.gif, etc."
        )
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("firmware/data"),
        help="Directory containing source GIF files (default: %(default)s).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("firmware/src/gif_assets.h"),
        help="Header file to write (default: %(default)s).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate inputs and compare generated content without writing the output file.",
    )
    return parser.parse_args()


def find_expression_files(data_dir: Path) -> dict[str, Path]:
    gifs = sorted(path for path in data_dir.iterdir() if path.is_file() and path.suffix.lower() == ".gif")
    if not gifs:
        raise ValueError(
            f"No GIF files found in {data_dir}. Existing headers were left untouched."
        )

    matched: dict[str, Path] = {}
    unknown: list[str] = []
    for path in gifs:
        match = FILENAME_PATTERN.fullmatch(path.name)
        if not match:
            unknown.append(path.name)
            continue

        expression = match.group("expression")
        if expression not in EXPECTED_EXPRESSIONS:
            unknown.append(path.name)
            continue

        if expression in matched:
            raise ValueError(
                f"Duplicate GIFs for expression '{expression}': "
                f"{matched[expression].name}, {path.name}"
            )
        matched[expression] = path

    if unknown:
        raise ValueError(
            "Unexpected GIF filenames: "
            + ", ".join(unknown)
            + ". Expected names like A_calm.gif, B_thinking.gif, etc."
        )

    missing = [name for name in EXPECTED_EXPRESSIONS if name not in matched]
    if missing:
        raise ValueError(
            "Missing GIF expressions: "
            + ", ".join(missing)
            + ". Refusing to generate a partial header."
        )

    return matched


def format_bytes(data: bytes) -> str:
    rows = []
    for offset in range(0, len(data), 12):
        chunk = data[offset : offset + 12]
        rows.append("    " + ", ".join(f"0x{byte:02x}" for byte in chunk))
    return ",\n".join(rows)


def render_header(expression_files: dict[str, Path]) -> str:
    blocks: list[str] = [HEADER_PREAMBLE]
    for expression in EXPECTED_EXPRESSIONS:
        data = expression_files[expression].read_bytes()
        blocks.append(
            f"const uint8_t face_gif_{expression}[] PROGMEM = {{\n{format_bytes(data)}\n}};\n"
            f"const size_t face_gif_{expression}_len = sizeof(face_gif_{expression});\n"
        )
    return "\n".join(blocks)


def write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        delete=False,
        prefix=f".{path.name}.",
        suffix=".tmp",
    ) as tmp:
        tmp.write(content)
        temp_path = Path(tmp.name)
    temp_path.replace(path)


def main() -> int:
    args = parse_args()
    data_dir = args.data_dir.resolve()
    output = args.output.resolve()

    try:
        expression_files = find_expression_files(data_dir)
        content = render_header(expression_files)
    except (FileNotFoundError, NotADirectoryError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    current = output.read_text(encoding="utf-8") if output.exists() else None
    if args.check:
        if current == content:
            print(f"{output} is up to date")
            return 0
        print(f"{output} is out of date")
        return 1

    if current == content:
        print(f"{output} is already up to date")
        return 0

    write_atomic(output, content)
    print(f"Wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
