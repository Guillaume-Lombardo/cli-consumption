from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path, PurePosixPath

MAX_RUNTIME_FILES = 5_000
MAX_RUNTIME_BYTES = 128 * 1024 * 1024
MAX_RUNTIME_ARCHIVE_BYTES = 32 * 1024 * 1024
MINIMUM_NODE = (20, 9)


class FrontendRuntimeError(RuntimeError):
    """A fixed, content-free dashboard runtime failure."""


def find_node_runtime() -> str:
    node = shutil.which("node")
    if node is None:
        raise FrontendRuntimeError("frontend_node_missing")
    try:
        result = subprocess.run(
            [node, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        raise FrontendRuntimeError("frontend_node_invalid") from None
    match = re.fullmatch(r"v(\d+)\.(\d+)\.\d+\s*", result.stdout)
    if result.returncode != 0 or match is None:
        raise FrontendRuntimeError("frontend_node_invalid")
    if (int(match.group(1)), int(match.group(2))) < MINIMUM_NODE:
        raise FrontendRuntimeError("frontend_node_unsupported")
    return node


@contextmanager
def materialize_frontend_runtime() -> Iterator[Path]:
    try:
        payload = files("cli_consumption").joinpath("web_runtime.zip").read_bytes()
    except (FileNotFoundError, OSError):
        raise FrontendRuntimeError("frontend_runtime_missing") from None

    if len(payload) > MAX_RUNTIME_ARCHIVE_BYTES:
        raise FrontendRuntimeError("frontend_runtime_invalid")

    with tempfile.TemporaryDirectory(prefix="cli-consumption-web-") as directory:
        destination = Path(directory)
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                members = archive.infolist()
                if (
                    len(members) > MAX_RUNTIME_FILES
                    or len({member.filename for member in members}) != len(members)
                    or sum(member.file_size for member in members) > MAX_RUNTIME_BYTES
                    or any(not _safe_runtime_member(member) for member in members)
                ):
                    raise FrontendRuntimeError("frontend_runtime_invalid")
                for member in members:
                    target = destination.joinpath(*PurePosixPath(member.filename).parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(member) as source, target.open("wb") as output:
                        shutil.copyfileobj(source, output)
        except (OSError, zipfile.BadZipFile):
            raise FrontendRuntimeError("frontend_runtime_invalid") from None
        runtime = destination / "runtime" / "apps" / "web"
        if not (runtime / "server.js").is_file():
            raise FrontendRuntimeError("frontend_runtime_invalid")
        yield runtime


def frontend_environment(
    *,
    api_url: str,
    origin: str,
    host: str,
    port: int,
    password: str,
    read_token: str,
    export_token: str,
    layout_token: str,
    session_secret: str,
) -> dict[str, str]:
    environment = {
        name: value
        for name in ("LANG", "LC_ALL", "PATH", "TMPDIR")
        if (value := os.environ.get(name)) is not None
    }
    environment.update(
        {
            "CLI_CONSUMPTION_API_URL": api_url,
            "CLI_CONSUMPTION_DASHBOARD_ORIGIN": origin,
            "CLI_CONSUMPTION_DASHBOARD_PASSWORD": password,
            "CLI_CONSUMPTION_EXPORT_TOKEN": export_token,
            "CLI_CONSUMPTION_LAYOUT_TOKEN": layout_token,
            "CLI_CONSUMPTION_READ_TOKEN": read_token,
            "CLI_CONSUMPTION_SESSION_SECRET": session_secret,
            "HOSTNAME": host,
            "NEXT_TELEMETRY_DISABLED": "1",
            "PORT": str(port),
        }
    )
    return environment


def start_frontend(
    node: str, runtime: Path, environment: Mapping[str, str]
) -> subprocess.Popen[bytes]:
    try:
        return subprocess.Popen(
            [node, "server.js"],
            cwd=runtime,
            env=dict(environment),
        )
    except OSError:
        raise FrontendRuntimeError("frontend_start_failed") from None


def stop_frontend(process: subprocess.Popen[bytes]) -> None:
    try:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    except (OSError, subprocess.SubprocessError):
        raise FrontendRuntimeError("frontend_stop_failed") from None


def _safe_runtime_member(member: zipfile.ZipInfo) -> bool:
    path = PurePosixPath(member.filename)
    file_type = member.external_attr >> 16 & 0o170000
    return (
        not member.is_dir()
        and "\\" not in member.filename
        and not path.is_absolute()
        and path.parts[:1] == ("runtime",)
        and ".." not in path.parts
        and file_type in {0, 0o100000}
    )
