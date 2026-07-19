from __future__ import annotations

import json
import struct
from pathlib import Path

from evolvable_memory import frontend


def _png_size(path: Path) -> tuple[int, int]:
    payload = path.read_bytes()
    assert payload[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", payload[16:24])


def test_frontend_declares_the_complete_memory_icon_set() -> None:
    index = (frontend._STATIC_DIRECTORY / "index.html").read_text(encoding="utf-8")

    assert 'href="./mark.svg?v=2"' in index
    assert 'href="./favicon-32.png"' in index
    assert 'href="./favicon-16.png"' in index
    assert 'href="./apple-touch-icon.png"' in index
    assert 'href="./site.webmanifest"' in index
    assert '<img src="./mark.svg?v=2" alt="" />' in index


def test_frontend_memory_icon_rasters_have_the_declared_dimensions() -> None:
    expected = {
        "favicon-16.png": (16, 16),
        "favicon-32.png": (32, 32),
        "apple-touch-icon.png": (180, 180),
        "icons/memory-192.png": (192, 192),
        "icons/memory-512.png": (512, 512),
    }

    for relative_path, dimensions in expected.items():
        assert _png_size(frontend._STATIC_DIRECTORY / relative_path) == dimensions


def test_frontend_manifest_uses_the_memory_brand_theme() -> None:
    manifest = json.loads(
        (frontend._STATIC_DIRECTORY / "site.webmanifest").read_text(encoding="utf-8")
    )

    assert manifest["short_name"] == "Memory"
    assert manifest["theme_color"] == "#2b3269"
    assert [icon["sizes"] for icon in manifest["icons"]] == ["192x192", "512x512"]
