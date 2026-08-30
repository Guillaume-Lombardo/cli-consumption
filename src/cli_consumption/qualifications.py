from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime

from cli_consumption.adapters.registry import ADAPTER_SPECS, AdapterSpec

DEFAULT_MAX_AGE_DAYS = 90


@dataclass(frozen=True, slots=True)
class QualificationProblem:
    """A static qualification contract that needs maintainer attention."""

    provider: str
    reason: str


def qualification_problems(
    *,
    as_of: date,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    specs: Sequence[AdapterSpec] = ADAPTER_SPECS,
) -> tuple[QualificationProblem, ...]:
    """Return missing, invalid, future, or stale qualification metadata."""
    if max_age_days < 0:
        raise ValueError("max_age_days must be non-negative")

    problems: list[QualificationProblem] = []
    for spec in specs:
        qualification = spec.qualification
        if qualification is None:
            problems.append(QualificationProblem(spec.name, "missing metadata"))
            continue
        try:
            qualified_on = date.fromisoformat(qualification.qualified_on)
        except ValueError:
            problems.append(QualificationProblem(spec.name, "invalid date"))
            continue
        age_days = (as_of - qualified_on).days
        if age_days < 0:
            problems.append(QualificationProblem(spec.name, "date is in the future"))
        elif age_days > max_age_days:
            problems.append(
                QualificationProblem(
                    spec.name,
                    f"qualification is {age_days} days old (maximum {max_age_days})",
                )
            )
    return tuple(problems)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fail when provider format qualifications need attention."
    )
    parser.add_argument(
        "--as-of",
        type=date.fromisoformat,
        default=None,
        help="UTC date used for the age check (YYYY-MM-DD; defaults to today).",
    )
    parser.add_argument(
        "--max-age-days",
        type=int,
        default=DEFAULT_MAX_AGE_DAYS,
        help=f"Maximum qualification age (default: {DEFAULT_MAX_AGE_DAYS}).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    as_of = args.as_of or datetime.now(UTC).date()
    try:
        problems = qualification_problems(
            as_of=as_of,
            max_age_days=args.max_age_days,
        )
    except ValueError as error:
        _parser().error(str(error))
    if problems:
        print("Provider qualification check failed:")
        for problem in problems:
            print(f"- {problem.provider}: {problem.reason}")
        return 1
    print(
        f"All {len(ADAPTER_SPECS)} provider qualifications are current "
        f"as of {as_of.isoformat()}."
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())
