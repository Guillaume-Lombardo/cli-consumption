from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
DEPLOYMENT = ROOT / "deploy" / "production"


def test_container_build_context_is_an_explicit_source_allowlist() -> None:
    patterns = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()

    assert patterns == [
        "**",
        "!pyproject.toml",
        "!uv.lock",
        "!README.md",
        "!LICENSE",
        "!NOTICE",
        "!src/",
        "!src/**",
    ]


def test_production_images_are_versioned_and_pinned_by_digest() -> None:
    dockerfile = (DEPLOYMENT / "Dockerfile").read_text(encoding="utf-8")
    compose = (DEPLOYMENT / "compose.yaml").read_text(encoding="utf-8")
    image_references = re.findall(r"(?:FROM|image:)\s+([^\s]+)", dockerfile + compose)

    assert len(image_references) == 5
    assert all(
        re.search(r":[^@\s]+@sha256:[0-9a-f]{64}$", image) for image in image_references
    )
    assert (
        "uv sync --frozen --no-dev --no-editable --extra postgres --extra server"
        in dockerfile
    )
    assert "USER 10001:10001" in dockerfile


def test_compose_requires_external_values_and_exposes_only_proxy() -> None:
    compose = (DEPLOYMENT / "compose.yaml").read_text(encoding="utf-8")
    example = (DEPLOYMENT / ".env.example").read_text(encoding="utf-8")

    assert compose.count(":?") == 5
    assert '"80:80"' in compose
    assert '"443:443"' in compose
    assert "8765:8765" not in compose
    assert "5432:5432" not in compose
    assert "internal: true" in compose
    assert "log_parameter_max_length_on_error=0" in compose
    assert "max_connections=30" in compose
    assert "CLI_CONSUMPTION_API_TOKEN=\n" in example
    assert "POSTGRES_PASSWORD=\n" in example


def test_reference_proxy_discards_untrusted_access_logs_and_bounds_connections() -> (
    None
):
    caddyfile = (DEPLOYMENT / "Caddyfile").read_text(encoding="utf-8")

    assert "output discard" in caddyfile
    assert "health_uri /ready" in caddyfile
    assert "health_uri /health" not in caddyfile
    assert "max_conns_per_host 16" in caddyfile
    assert "max_size 32MB" in caddyfile
    assert "max_header_size 32KB" in caddyfile


def test_deployment_examples_contain_no_resolved_secret_or_private_artifact() -> None:
    combined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(DEPLOYMENT.rglob("*"))
        if path.is_file()
    )

    forbidden = (
        "BEGIN PRIVATE KEY",
        "ghp_",
        "sk-",
        "privacy-canary",
        "/home/",
        "usage.sqlite",
    )
    assert not any(value in combined for value in forbidden)
