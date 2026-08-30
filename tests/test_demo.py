from __future__ import annotations

import os
import struct
import subprocess
import sys
from pathlib import Path


def test_public_demo_is_reproducible_and_synthetic(tmp_path: Path) -> None:
    project_root = Path(__file__).parents[1]
    tracked_dashboard = project_root / "docs" / "demo" / "dashboard.html"
    generated_dashboard = tmp_path / "dashboard.html"
    environment = os.environ.copy()
    environment["HOME"] = str(tmp_path / "empty-home")

    result = subprocess.run(
        [
            sys.executable,
            str(project_root / "docs" / "demo" / "generate.py"),
            "--output",
            str(generated_dashboard),
        ],
        cwd=project_root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert generated_dashboard.read_bytes() == tracked_dashboard.read_bytes()
    html = generated_dashboard.read_text(encoding="utf-8")
    assert "cli-consumption-public-demo" not in html
    assert "demo-laptop" in html
    assert "demo-api" in html
    assert "privacy canary" not in html
    assert "secret" not in html.lower()
    assert "fetch(" not in html
    assert "<script src" not in html
    assert not list((project_root / "docs" / "demo").glob("*.sqlite*"))


def test_readme_preview_is_a_real_png_with_expected_dimensions() -> None:
    project_root = Path(__file__).parents[1]
    preview = project_root / "docs" / "demo" / "dashboard.png"
    contents = preview.read_bytes()

    assert contents.startswith(b"\x89PNG\r\n\x1a\n")
    width, height = struct.unpack(">II", contents[16:24])
    assert (width, height) == (1440, 900)
