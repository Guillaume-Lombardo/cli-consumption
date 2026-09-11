from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path
from types import ModuleType

import pytest


def _load_build_module() -> ModuleType:
    path = Path(__file__).parents[1] / "tools" / "build_web_runtime.py"
    specification = importlib.util.spec_from_file_location("build_web_runtime", path)
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


build_web_runtime = _load_build_module()


@pytest.mark.parametrize(
    "compression",
    [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED],
)
def test_runtime_check_compares_uncompressed_contents(
    monkeypatch, tmp_path: Path, compression: int
) -> None:
    staging = tmp_path / "staging"
    (staging / "nested").mkdir(parents=True)
    (staging / "nested" / "server.js").write_bytes(b"dashboard-runtime")
    archive_path = tmp_path / "runtime.zip"
    with zipfile.ZipFile(archive_path, mode="w", compression=compression) as archive:
        archive.writestr("runtime/nested/server.js", b"dashboard-runtime")
    monkeypatch.setattr(build_web_runtime, "OUTPUT", archive_path)

    build_web_runtime._check_zip_contents(staging)


def test_runtime_check_rejects_stale_contents(monkeypatch, tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "server.js").write_bytes(b"current")
    archive_path = tmp_path / "runtime.zip"
    with zipfile.ZipFile(archive_path, mode="w") as archive:
        archive.writestr("runtime/server.js", b"stale")
    monkeypatch.setattr(build_web_runtime, "OUTPUT", archive_path)

    with pytest.raises(SystemExit, match="out of date"):
        build_web_runtime._check_zip_contents(staging)
