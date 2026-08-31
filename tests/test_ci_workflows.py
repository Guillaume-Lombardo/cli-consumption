from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]
CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
RELEASE = (ROOT / ".github" / "workflows" / "release.yaml").read_text(encoding="utf-8")
SECURITY = (ROOT / ".github" / "workflows" / "security.yml").read_text(encoding="utf-8")
PRE_COMMIT = (ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
AGENT_INSTRUCTIONS = (ROOT / "AGENTS.md").read_text(encoding="utf-8")


def test_ci_and_release_run_static_quality_gates_once_via_pre_commit() -> None:
    step_name = "Run file hygiene, Ruff format/check, and ty once via pre-commit"
    pre_commit_command = "uv run pre-commit run --all-files --show-diff-on-failure"
    duplicate_commands = (
        "uv run ruff format --check .",
        "uv run ruff check .",
        "uv run ty check",
    )

    for workflow in (CI, RELEASE):
        assert workflow.count(step_name) == 1
        assert workflow.count(pre_commit_command) == 1
        assert not any(command in workflow for command in duplicate_commands)

    assert PRE_COMMIT.count("- id: ruff-check") == 1
    assert PRE_COMMIT.count("- id: ruff-format") == 1
    assert PRE_COMMIT.count("- id: ty") == 1
    for reproducible_command in (
        "uv run pre-commit run --all-files",
        "uv run ruff format --check .",
        "uv run ruff check .",
        "uv run ty check",
    ):
        assert reproducible_command in AGENT_INSTRUCTIONS


def test_ci_keeps_compatibility_postgresql_build_and_minimal_smoke_coverage() -> None:
    assert "postgres:" in CI
    assert 'python-version: ["3.11", "3.12", "3.13", "3.14"]' in CI
    assert "minimum-server-stack:" in CI
    assert 'python-version: "3.11"' in CI
    assert "--python 3.11" in CI
    assert "fastapi==0.115.0" in CI
    assert CI.count("uv build") == 1
    assert CI.count("Smoke test the minimal wheel") == 1


def test_release_keeps_tests_build_and_minimal_wheel_smoke_test() -> None:
    assert RELEASE.count("uv run pytest --cov --cov-report=term-missing") == 1
    assert RELEASE.count("uv build") == 1
    assert RELEASE.count("Smoke test the minimal wheel") == 1
    assert "Upload distributions" in RELEASE
    assert "Publish distributions to PyPI" in RELEASE


def test_release_publishes_built_distributions_on_github_after_pypi() -> None:
    publish = RELEASE.index("  publish:\n")
    github_release = RELEASE.index("  github-release:\n")
    assert publish < github_release

    job = RELEASE[github_release:]
    assert "needs: [detect-version, build, tag, publish]" in job
    assert "contents: write" in job
    assert "actions/download-artifact@" in job
    assert 'gh release create "${tag}" --draft --verify-tag' in job
    assert "artifacts=(dist/*.whl dist/*.tar.gz)" in job
    assert 'gh release upload "${tag}" "${artifact}"' in job
    assert '[[ "${actual_assets}" != "${expected_assets}" ]]' in job
    assert 'gh release edit "${tag}" --draft=false --latest --verify-tag' in job


def test_security_audits_locked_dependencies_without_the_editable_project() -> None:
    step = SECURITY.split(
        "      - name: Audit installed locked dependencies\n", maxsplit=1
    )[1].split("      - name:", maxsplit=1)[0]
    run_script = step.split("        run: |\n", maxsplit=1)[1]
    commands: list[str] = []
    command = ""
    for line in run_script.splitlines():
        part = line.strip()
        command = f"{command} {part}".strip()
        if part.endswith("\\"):
            command = command[:-1].rstrip()
        else:
            commands.append(command)
            command = ""

    assert commands == [
        "uv export --locked --all-extras --all-groups --no-emit-project "
        '--no-hashes --output-file "${RUNNER_TEMP}/locked-requirements.txt"',
        "uv run pip-audit --strict "
        '--requirement "${RUNNER_TEMP}/locked-requirements.txt"',
    ]
