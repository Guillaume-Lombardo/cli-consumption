from __future__ import annotations

import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path


def test_python_support_and_optional_dependencies_are_declared() -> None:
    project_root = Path(__file__).parents[1]
    configuration = tomllib.loads(
        (project_root / "pyproject.toml").read_text(encoding="utf-8")
    )
    project = configuration["project"]

    assert project["requires-python"] == ">=3.11"
    assert "Programming Language :: Python :: 3.11" in project["classifiers"]
    assert project["dependencies"] == [
        "alembic>=1.14",
        "pydantic>=2.10",
        "sqlalchemy>=2.0",
        "typer>=0.15",
    ]
    assert project["optional-dependencies"] == {
        "postgres": ["psycopg[binary]>=3.2"],
        "server": ["fastapi>=0.115", "uvicorn>=0.34"],
        "snapshots": ["cryptography>=45"],
        "sync": ["httpx>=0.27"],
    }
    assert project["urls"]["Changelog"] == (
        "https://github.com/Guillaume-Lombardo/cli-consumption/blob/main/CHANGELOG.md"
    )
    assert configuration["tool"]["ruff"]["target-version"] == "py311"


def test_distribution_artifact_contract(tmp_path: Path) -> None:
    project_root = Path(__file__).parents[1]
    distributions = tmp_path / "dist"

    _run_build("--sdist", "--out-dir", str(distributions), str(project_root))
    sdist = next(distributions.glob("*.tar.gz"))
    # Building from the archive, rather than the checkout, proves that the minimal
    # sdist contains everything Hatch needs to reconstruct the release wheel.
    _run_build("--wheel", "--out-dir", str(distributions), str(sdist))
    wheel = next(distributions.glob("*.whl"))

    distribution_root = sdist.name.removesuffix(".tar.gz")
    expected_source_files = {
        f"{distribution_root}/{path.relative_to(project_root).as_posix()}"
        for path in (project_root / "src" / "cli_consumption").rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }
    with tarfile.open(sdist, mode="r:gz") as archive:
        sdist_files = {
            member.name for member in archive.getmembers() if member.isfile()
        }

    assert sdist_files == expected_source_files | {
        # Hatchling deliberately carries the active VCS ignore file into sdists so
        # downstream rebuilds keep excluding local build and cache artifacts.
        f"{distribution_root}/.gitignore",
        f"{distribution_root}/CHANGELOG.md",
        f"{distribution_root}/LICENSE",
        f"{distribution_root}/NOTICE",
        f"{distribution_root}/PKG-INFO",
        f"{distribution_root}/README.md",
        f"{distribution_root}/pyproject.toml",
    }

    expected_package_files = {
        path.relative_to(project_root / "src").as_posix()
        for path in (project_root / "src" / "cli_consumption").rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }
    with zipfile.ZipFile(wheel) as archive:
        wheel_files = set(archive.namelist())

    wheel_metadata = f"{distribution_root}.dist-info"
    assert wheel_files == expected_package_files | {
        f"{wheel_metadata}/METADATA",
        f"{wheel_metadata}/RECORD",
        f"{wheel_metadata}/WHEEL",
        f"{wheel_metadata}/entry_points.txt",
        f"{wheel_metadata}/licenses/LICENSE",
        f"{wheel_metadata}/licenses/NOTICE",
    }

    smoke = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            (
                "import sys; "
                f"sys.path.insert(0, {str(wheel)!r}); "
                "import cli_consumption, cli_consumption.migrations, "
                "cli_consumption.snapshot_files; "
                "from cli_consumption.dashboard import "
                "_dashboard_calculations_script, _react_dashboard_script, "
                "_react_dashboard_styles; "
                "assert cli_consumption.__version__ != '0.0.0'; "
                "assert 'createDashboardCalculations' in "
                "_dashboard_calculations_script(); "
                "assert 'offline_dashboard_root_missing' in _react_dashboard_script(); "
                "assert '.dashboard-shell' in _react_dashboard_styles()"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert smoke.returncode == 0, smoke.stderr

    repeated_distributions = tmp_path / "repeated-dist"
    _run_build("--sdist", "--out-dir", str(repeated_distributions), str(project_root))
    repeated_sdist = next(repeated_distributions.glob("*.tar.gz"))
    _run_build(
        "--wheel",
        "--out-dir",
        str(repeated_distributions),
        str(repeated_sdist),
    )
    repeated_wheel = next(repeated_distributions.glob("*.whl"))
    assert repeated_sdist.read_bytes() == sdist.read_bytes()
    assert repeated_wheel.read_bytes() == wheel.read_bytes()


def _run_build(*arguments: str) -> None:
    result = subprocess.run(
        ["uv", "build", "--no-progress", *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
