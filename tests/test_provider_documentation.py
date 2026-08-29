from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from cli_consumption.adapters.registry import ADAPTER_SPECS, AdapterSpec

PROJECT_ROOT = Path(__file__).parents[1]
README = PROJECT_ROOT / "README.md"
PROVIDER_SUPPORT = PROJECT_ROOT / "docs" / "provider-support.md"


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _table(path: Path, required_columns: set[str]) -> list[dict[str, str]]:
    """Read a Markdown table by named columns, independent of spacing/alignment."""
    lines = path.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines[:-1]):
        if not line.lstrip().startswith("|"):
            continue
        columns = _cells(line)
        if set(columns) != required_columns:
            continue
        separator = _cells(lines[index + 1])
        assert len(separator) == len(columns)
        assert all(re.fullmatch(r":?-{3,}:?", cell) for cell in separator)
        rows: list[dict[str, str]] = []
        for row_line in lines[index + 2 :]:
            if not row_line.lstrip().startswith("|"):
                break
            values = _cells(row_line)
            assert len(values) == len(columns)
            rows.append(dict(zip(columns, values, strict=True)))
        return rows
    raise AssertionError(f"No table with columns {sorted(required_columns)} in {path}")


def _code_values(cell: str) -> tuple[str, ...]:
    values = tuple(re.findall(r"`([^`]+)`", cell))
    assert values or cell == "—"
    return values


def _single_code(cell: str) -> str:
    values = _code_values(cell)
    assert len(values) == 1
    return values[0]


def _assert_source_matches_default(spec: AdapterSpec, source: str) -> None:
    default_home = spec.default_home.rstrip("/")
    expected_root = (
        default_home if default_home.startswith("/") else f"~/{default_home}"
    )
    actual = source.rstrip("/")
    assert actual == expected_root or actual.startswith(f"{expected_root}/")


def test_provider_matrices_match_the_canonical_registry() -> None:
    common = {
        "Provider name",
        "Aliases",
        "Default local source",
        "Token semantics",
    }
    readme_rows = _table(README, common | {"CLI", "Particularities and limits"})
    support_rows = _table(PROVIDER_SUPPORT, common | {"Provider", "Status"})

    expected_names = {spec.name for spec in ADAPTER_SPECS}
    assert Counter(_single_code(row["Provider name"]) for row in readme_rows) == {
        name: 1 for name in expected_names
    }
    assert Counter(_single_code(row["Provider name"]) for row in support_rows) == {
        name: 1 for name in expected_names
    }

    readme_by_name = {_single_code(row["Provider name"]): row for row in readme_rows}
    support_by_name = {_single_code(row["Provider name"]): row for row in support_rows}
    for spec in ADAPTER_SPECS:
        readme = readme_by_name[spec.name]
        support = support_by_name[spec.name]
        assert _code_values(readme["Aliases"]) == spec.aliases
        assert _code_values(support["Aliases"]) == spec.aliases
        assert _single_code(readme["Token semantics"]) == spec.token_semantics
        assert _single_code(support["Token semantics"]) == spec.token_semantics
        assert _single_code(support["Status"]) == spec.support

        readme_source = _single_code(readme["Default local source"])
        support_source = _single_code(support["Default local source"])
        assert readme_source == support_source
        _assert_source_matches_default(spec, readme_source)


def test_every_provider_has_exactly_one_qualified_support_section() -> None:
    rows = _table(
        PROVIDER_SUPPORT,
        {
            "Provider",
            "Provider name",
            "Aliases",
            "Status",
            "Default local source",
            "Token semantics",
        },
    )
    text = PROVIDER_SUPPORT.read_text(encoding="utf-8")
    headings = re.findall(r"^## ([^\n]+)$", text, flags=re.MULTILINE)
    documented_providers = [row["Provider"] for row in rows]

    assert Counter(headings) == Counter({name: 1 for name in documented_providers})
    for index, heading in enumerate(headings):
        start = text.index(f"## {heading}") + len(f"## {heading}")
        next_heading = text.find("\n## ", start)
        section = text[start : next_heading if next_heading >= 0 else len(text)]
        paragraphs = [part for part in section.strip().split("\n\n") if part.strip()]
        assert len(paragraphs) >= 1, (index, heading)
        assert len(section.split()) >= 35, (index, heading)
