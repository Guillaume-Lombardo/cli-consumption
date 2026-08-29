from __future__ import annotations

import inspect
from pathlib import Path

from cli_consumption.adapters._shared import ProviderDataLimitError
from cli_consumption.adapters.base import UnsupportedProviderFormat
from cli_consumption.adapters.registry import (
    ADAPTER_SPECS,
    AdapterSpec,
    default_source_path,
    diagnose_provider,
    has_provider_data,
    resolve_adapter_spec,
)
from cli_consumption.models import Snapshot


class _DetectedAdapter:
    name = "detected"

    def collect(
        self,
        sources: list[tuple[str, Path]],
        project_mappings: list[tuple[str, str]],
    ) -> Snapshot:
        return Snapshot(provider=self.name)


class _CompatibleAdapter:
    name = "compatible"

    def collect(
        self,
        sources: list[tuple[str, Path]],
        project_mappings: list[tuple[str, str]],
    ) -> Snapshot:
        return Snapshot(provider=self.name, conversations=[{"id": "synthetic"}])


class _DegradedAdapter:
    name = "degraded"

    def collect(
        self,
        sources: list[tuple[str, Path]],
        project_mappings: list[tuple[str, str]],
    ) -> Snapshot:
        return Snapshot(provider=self.name, malformed_records=1)


class _UnsupportedAdapter:
    name = "unsupported"

    def collect(
        self,
        sources: list[tuple[str, Path]],
        project_mappings: list[tuple[str, str]],
    ) -> Snapshot:
        raise UnsupportedProviderFormat("synthetic schema is unsupported")


class _LimitedAdapter:
    name = "limited"

    def collect(
        self,
        sources: list[tuple[str, Path]],
        project_mappings: list[tuple[str, str]],
    ) -> Snapshot:
        raise ProviderDataLimitError("provider_file_too_large")


def test_registry_is_complete_unique_and_consistent() -> None:
    expected = [
        "aider",
        "amazon-q",
        "amp",
        "codex",
        "copilot",
        "continue",
        "crush",
        "cursor",
        "gemini",
        "goose",
        "grok",
        "claude",
        "cline",
        "kilo",
        "kimi",
        "mistral-vibe",
        "opencode",
        "openhands",
        "pi",
        "plandex",
        "qwen",
    ]
    assert [spec.name for spec in ADAPTER_SPECS] == expected
    assert len({spec.name for spec in ADAPTER_SPECS}) == len(ADAPTER_SPECS)
    assert all(spec.adapter_type().name == spec.name for spec in ADAPTER_SPECS)
    assert all(spec.markers for spec in ADAPTER_SPECS)
    assert all(spec.support == "supported" for spec in ADAPTER_SPECS)
    assert all(
        inspect.getsource(spec.adapter_type.collect).count("ProviderInputBudget()") == 1
        for spec in ADAPTER_SPECS
    )
    assert {spec.name: spec.token_semantics for spec in ADAPTER_SPECS} == {
        "aider": "additive",
        "amazon-q": "unavailable",
        "amp": "additive",
        "codex": "additive",
        "copilot": "conversation-aggregate",
        "continue": "additive",
        "crush": "context-snapshot",
        "cursor": "unavailable",
        "gemini": "additive",
        "goose": "additive",
        "grok": "additive",
        "claude": "additive",
        "cline": "additive",
        "kilo": "additive",
        "kimi": "additive",
        "mistral-vibe": "conversation-aggregate",
        "opencode": "additive",
        "openhands": "additive",
        "pi": "additive",
        "plandex": "additive",
        "qwen": "additive",
    }

    registered_names = {
        name for spec in ADAPTER_SPECS for name in (spec.name, *spec.aliases)
    }
    assert len(registered_names) == sum(1 + len(spec.aliases) for spec in ADAPTER_SPECS)


def test_registry_resolves_canonical_names_and_aliases() -> None:
    claude = resolve_adapter_spec("claude")
    assert claude is not None
    assert resolve_adapter_spec("claude-code") is claude
    assert resolve_adapter_spec("unknown") is None


def test_every_registered_adapter_has_a_synthetic_privacy_canary_contract() -> None:
    tests = Path(__file__).parent
    for spec in ADAPTER_SPECS:
        test_module = tests / f"test_{spec.name.replace('-', '_')}_adapter.py"
        source = test_module.read_text(encoding="utf-8")
        assert "CANARY" in source or "privacy canary" in source
        assert "snapshot.to_dict()" in source
        assert "not in" in source


def test_adapter_specs_are_immutable() -> None:
    assert AdapterSpec.__dataclass_params__.frozen


def test_default_source_path_handles_relative_and_absolute_homes(
    tmp_path: Path,
) -> None:
    codex = resolve_adapter_spec("codex")
    plandex = resolve_adapter_spec("plandex")
    assert codex is not None
    assert plandex is not None

    assert default_source_path(codex, tmp_path) == (tmp_path / ".codex").resolve()
    assert default_source_path(plandex, tmp_path) == Path("/plandex-server")


def test_provider_markers_support_direct_paths_and_globs(tmp_path: Path) -> None:
    direct = AdapterSpec("direct", ADAPTER_SPECS[0].adapter_type, ".direct", ("data",))
    globbed = AdapterSpec(
        "globbed",
        ADAPTER_SPECS[0].adapter_type,
        ".globbed",
        ("sessions/*/events.jsonl",),
    )
    assert not has_provider_data(direct, tmp_path)
    assert not has_provider_data(globbed, tmp_path)

    (tmp_path / "data").mkdir()
    event = tmp_path / "sessions" / "one" / "events.jsonl"
    event.parent.mkdir(parents=True)
    event.touch()

    assert has_provider_data(direct, tmp_path)
    assert has_provider_data(globbed, tmp_path)


def test_provider_diagnostics_cover_every_bounded_status(tmp_path: Path) -> None:
    missing = AdapterSpec("missing", _DetectedAdapter, ".missing", ("marker",))
    specs = (
        AdapterSpec("detected", _DetectedAdapter, ".detected", ("marker",)),
        AdapterSpec("compatible", _CompatibleAdapter, ".compatible", ("marker",)),
        AdapterSpec("degraded", _DegradedAdapter, ".degraded", ("marker",)),
        AdapterSpec("unsupported", _UnsupportedAdapter, ".unsupported", ("marker",)),
        AdapterSpec("limited", _LimitedAdapter, ".limited", ("marker",)),
    )
    for spec in specs:
        (tmp_path / spec.name).mkdir()
        (tmp_path / spec.name / "marker").touch()

    assert diagnose_provider(missing, tmp_path / "missing").status == "no-data"
    assert [diagnose_provider(spec, tmp_path / spec.name).status for spec in specs] == [
        "detected",
        "compatible",
        "degraded",
        "unsupported-schema",
        "degraded",
    ]
    diagnostic = diagnose_provider(specs[-1], tmp_path / "limited").to_dict()
    assert "provider_file_too_large" not in str(diagnostic)
