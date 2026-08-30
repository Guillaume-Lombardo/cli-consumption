from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from cli_consumption.adapters.registry import ADAPTER_SPECS, AdapterSpec
from cli_consumption.qualifications import main, qualification_problems

PROJECT_ROOT = Path(__file__).parents[1]


def test_every_provider_has_verifiable_synthetic_qualification_provenance() -> None:
    for spec in ADAPTER_SPECS:
        qualification = spec.qualification
        assert qualification is not None, spec.name
        assert qualification.version.strip(), spec.name
        assert qualification.format.strip(), spec.name
        assert qualification.limitations.strip(), spec.name
        assert date.fromisoformat(qualification.qualified_on), spec.name
        assert qualification.provenance.startswith("https://"), spec.name
        if "github.com" in qualification.provenance:
            assert "/tree/" in qualification.provenance, spec.name
        else:
            assert spec.name in {"amp", "cursor"}, spec.name

        fixture = PROJECT_ROOT / qualification.fixture
        assert fixture.is_file(), spec.name
        fixture_source = fixture.read_text(encoding="utf-8")
        assert "CANARY" in fixture_source or "privacy canary" in fixture_source
        assert "snapshot.to_dict()" in fixture_source


def test_qualification_age_boundary_is_deterministic() -> None:
    assert qualification_problems(as_of=date(2026, 11, 28)) == ()
    problems = qualification_problems(as_of=date(2026, 11, 29))
    assert len(problems) == len(ADAPTER_SPECS)
    assert {problem.reason for problem in problems} == {
        "qualification is 91 days old (maximum 90)"
    }


def test_qualification_check_rejects_missing_metadata() -> None:
    missing = AdapterSpec("missing", ADAPTER_SPECS[0].adapter_type, ".missing", ("x",))
    problem = qualification_problems(as_of=date(2026, 8, 30), specs=(missing,))[0]
    assert (problem.provider, problem.reason) == ("missing", "missing metadata")


def test_qualification_check_rejects_invalid_future_dates_and_negative_age() -> None:
    original = ADAPTER_SPECS[0]
    assert original.qualification is not None
    invalid = replace(
        original,
        qualification=replace(original.qualification, qualified_on="not-a-date"),
    )
    future = replace(
        original,
        qualification=replace(original.qualification, qualified_on="2026-08-31"),
    )

    problems = qualification_problems(as_of=date(2026, 8, 30), specs=(invalid, future))

    assert [(problem.provider, problem.reason) for problem in problems] == [
        ("aider", "invalid date"),
        ("aider", "date is in the future"),
    ]
    with pytest.raises(ValueError, match="max_age_days must be non-negative"):
        qualification_problems(as_of=date(2026, 8, 30), max_age_days=-1)


def test_qualification_command_reports_only_static_registry_metadata(capsys) -> None:
    assert main(["--as-of", "2026-11-28"]) == 0
    output = capsys.readouterr().out
    assert output == ("All 21 provider qualifications are current as of 2026-11-28.\n")

    assert main(["--as-of", "2026-11-29"]) == 1
    output = capsys.readouterr().out
    assert "aider: qualification is 91 days old" in output
    assert "/home/" not in output
    assert "CANARY" not in output
