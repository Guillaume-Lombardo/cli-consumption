from __future__ import annotations

import subprocess
import tempfile
from importlib.util import find_spec
from pathlib import Path


def run_cli(*args: str, expected: int = 0) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["cli-consumption", *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != expected:
        raise AssertionError((args, result.stdout, result.stderr))
    return result


for package in ("fastapi", "httpx", "psycopg", "uvicorn"):
    if find_spec(package) is not None:
        raise AssertionError(f"optional dependency unexpectedly installed: {package}")

run_cli("providers")
run_cli("collect", "--help")
run_cli("export", "--help")

with tempfile.TemporaryDirectory() as temporary_directory:
    root = Path(temporary_directory)
    source = root / "source"
    (source / "sessions").mkdir(parents=True)
    database = root / "minimal.sqlite"
    run_cli(
        "collect",
        "--provider",
        "codex",
        "--source",
        f"minimal={source}",
        "--database",
        str(database),
    )
    run_cli(
        "export",
        "--database",
        str(database),
        "--output",
        str(root / "reports"),
    )

    optional_commands = (
        ("sync", ("--endpoint", "https://collector.test"), "sync"),
        ("serve", (), "server"),
        (
            "export",
            (
                "--database",
                "postgresql://usage@db/cli_consumption",
                "--output",
                str(root / "postgres-report"),
            ),
            "postgres",
        ),
    )
    for command, arguments, extra in optional_commands:
        result = run_cli(command, *arguments, expected=2)
        output = result.stdout + result.stderr
        if f"cli-consumption[{extra}]" not in output or "Traceback" in output:
            raise AssertionError(output)
