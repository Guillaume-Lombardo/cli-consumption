from __future__ import annotations

import http.client
import io
import socket
import subprocess
import time
import zipfile
from pathlib import Path
from typing import cast

import pytest

import cli_consumption.frontend as frontend
from cli_consumption.frontend import FrontendRuntimeError


def test_bundled_frontend_runtime_is_portable_and_complete() -> None:
    with frontend.materialize_frontend_runtime() as runtime:
        assert (runtime / "server.js").is_file()
        assert (runtime / ".next" / "static").is_dir()
        assert (runtime.parents[1] / "node_modules" / "next").is_dir()
        assert not list(runtime.parents[1].rglob("*.node"))
        assert not list(runtime.parents[1].rglob("*.so"))
        for path in runtime.parents[1].rglob("*"):
            if path.is_file():
                assert b"/home/" not in path.read_bytes()


def test_bundled_frontend_runtime_serves_the_login_page() -> None:
    try:
        node = frontend.find_node_runtime()
    except FrontendRuntimeError as error:
        pytest.skip(str(error))

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    credentials = (
        "test-dashboard-password",
        "test-read-token",
        "test-export-token",
        "test-layout-token",
        "test-session-secret-with-at-least-thirty-two-bytes",
    )
    environment = frontend.frontend_environment(
        api_url="http://127.0.0.1:9",
        origin=origin,
        host="127.0.0.1",
        port=port,
        password=credentials[0],
        read_token=credentials[1],
        export_token=credentials[2],
        layout_token=credentials[3],
        session_secret=credentials[4],
    )

    with frontend.materialize_frontend_runtime() as runtime:
        process = frontend.start_frontend(node, runtime, environment)
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    pytest.fail("The bundled frontend exited before becoming ready.")
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
                try:
                    connection.request("GET", "/login")
                    response = connection.getresponse()
                    body = response.read()
                except (OSError, http.client.HTTPException):
                    time.sleep(0.05)
                    continue
                finally:
                    connection.close()
                assert response.status == 200
                assert b"CLI Consumption" in body
                break
            else:
                pytest.fail("The bundled frontend did not become ready in time.")
        finally:
            frontend.stop_frontend(process)


def test_frontend_runtime_rejects_archive_traversal(monkeypatch) -> None:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, mode="w") as archive:
        archive.writestr("runtime/../CANARY_ARCHIVE_SECRET", "do-not-extract")

    class Resource:
        def joinpath(self, _name: str) -> Resource:
            return self

        def read_bytes(self) -> bytes:
            return payload.getvalue()

    monkeypatch.setattr(frontend, "files", lambda _package: Resource())

    with (
        pytest.raises(FrontendRuntimeError, match=r"^frontend_runtime_invalid$"),
        frontend.materialize_frontend_runtime(),
    ):
        pass


def test_frontend_environment_forwards_only_required_process_values(
    monkeypatch,
) -> None:
    monkeypatch.setenv("CANARY_UNRELATED_SECRET", "must-not-cross-process-boundary")
    values = (
        "dashboard-value-09c1",
        "read-value-09c1",
        "export-value-09c1",
        "layout-value-09c1",
        "session-value-with-at-least-thirty-two-bytes",
    )
    environment = frontend.frontend_environment(
        api_url="http://127.0.0.1:8765",
        origin="http://127.0.0.1:3000",
        host="127.0.0.1",
        port=3000,
        password=values[0],
        read_token=values[1],
        export_token=values[2],
        layout_token=values[3],
        session_secret=values[4],
    )

    assert "CANARY_UNRELATED_SECRET" not in environment
    assert environment["NEXT_TELEMETRY_DISABLED"] == "1"
    assert environment["CLI_CONSUMPTION_API_URL"] == "http://127.0.0.1:8765"


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("v20.8.0\n", "frontend_node_unsupported"),
        ("not-a-version\n", "frontend_node_invalid"),
    ],
)
def test_node_runtime_version_is_bounded(
    monkeypatch, version: str, expected: str
) -> None:
    monkeypatch.setattr(frontend.shutil, "which", lambda _name: "/usr/bin/node")
    monkeypatch.setattr(
        frontend.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, version, ""),
    )

    with pytest.raises(FrontendRuntimeError, match=f"^{expected}$"):
        frontend.find_node_runtime()


def test_start_frontend_uses_the_materialized_runtime(
    monkeypatch, tmp_path: Path
) -> None:
    observed: dict[str, object] = {}

    def popen(arguments, **kwargs):
        observed.update(arguments=arguments, **kwargs)
        return "process"

    monkeypatch.setattr(frontend.subprocess, "Popen", popen)
    environment = {"PATH": "/usr/bin"}

    process = frontend.start_frontend("/usr/bin/node", tmp_path, environment)

    assert process == "process"
    assert observed == {
        "arguments": ["/usr/bin/node", "server.js"],
        "cwd": tmp_path,
        "env": environment,
    }


def test_stop_frontend_kills_a_process_that_ignores_termination() -> None:
    class Process:
        terminated = False
        killed = False
        waits = 0

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, *, timeout: int) -> int:
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired("node", timeout)
            return 0

        def kill(self) -> None:
            self.killed = True

    raw_process = Process()
    process = cast(subprocess.Popen[bytes], raw_process)

    frontend.stop_frontend(process)

    assert raw_process.terminated is True
    assert raw_process.killed is True
    assert raw_process.waits == 2
