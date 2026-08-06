from pathlib import Path

import pytest

from scripts import generate_gif_assets


def write_expression_set(data_dir: Path) -> dict[str, bytes]:
    data_dir.mkdir()
    contents: dict[str, bytes] = {}
    for index, expression in enumerate(generate_gif_assets.EXPECTED_EXPRESSIONS):
        payload = b"GIF89a" + bytes([index])
        contents[expression] = payload
        (data_dir / f"{chr(ord('A') + index)}_{expression}.gif").write_bytes(payload)
    return contents


def test_find_expression_files_requires_complete_known_set(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    write_expression_set(data_dir)

    files = generate_gif_assets.find_expression_files(data_dir)

    assert tuple(files) == generate_gif_assets.EXPECTED_EXPRESSIONS

    (data_dir / "Z_unknown.gif").write_bytes(b"GIF89a")
    with pytest.raises(ValueError, match="Unexpected GIF filenames"):
        generate_gif_assets.find_expression_files(data_dir)


def test_find_expression_files_does_not_accept_partial_input(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "A_calm.gif").write_bytes(b"GIF89a")

    with pytest.raises(ValueError, match="Missing GIF expressions"):
        generate_gif_assets.find_expression_files(data_dir)


def test_render_header_is_deterministic_and_write_is_atomic(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    contents = write_expression_set(data_dir)
    files = generate_gif_assets.find_expression_files(data_dir)

    first = generate_gif_assets.render_header(files)
    second = generate_gif_assets.render_header(files)

    assert first == second
    assert first.startswith("#pragma once")
    for expression, payload in contents.items():
        assert f"face_gif_{expression}[]" in first
        assert generate_gif_assets.format_bytes(payload) in first

    output = tmp_path / "generated" / "gif_assets.h"
    generate_gif_assets.write_atomic(output, first)
    assert output.read_text(encoding="utf-8") == first
