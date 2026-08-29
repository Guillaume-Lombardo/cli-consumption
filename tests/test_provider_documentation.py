from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from cli_consumption.adapters.registry import ADAPTER_SPECS

PROJECT_ROOT = Path(__file__).parents[1]
README = PROJECT_ROOT / "README.md"
PROVIDER_SUPPORT = PROJECT_ROOT / "docs" / "provider-support.md"


def _cells(line: str) -> list[str]:
    cells: list[str] = []
    cell: list[str] = []
    code_delimiter: int | None = None
    text = line.strip()
    index = 0
    while index < len(text):
        character = text[index]
        if character == "\\" and index + 1 < len(text):
            cell.append(character)
            cell.append(text[index + 1])
            index += 2
            continue
        if character == "`":
            run_end = index + 1
            while run_end < len(text) and text[run_end] == "`":
                run_end += 1
            run_length = run_end - index
            if code_delimiter is None:
                code_delimiter = run_length
            elif code_delimiter == run_length:
                code_delimiter = None
            cell.extend(text[index:run_end])
            index = run_end
            continue
        if character == "|" and code_delimiter is None:
            cells.append("".join(cell).strip())
            cell = []
        else:
            cell.append(character)
        index += 1
    cells.append("".join(cell).strip())
    if cells and not cells[0]:
        cells.pop(0)
    if cells and not cells[-1]:
        cells.pop()
    return cells


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
        assert readme_source == support_source == spec.documented_source


def test_markdown_table_parser_preserves_escaped_and_inline_code_pipes(
    tmp_path: Path,
) -> None:
    document = tmp_path / "table.md"
    document.write_text(
        """\
| Name | Narrative |
| --- | --- |
| `provider` | Escaped \\| pipe, `inline|code`, and ``inline `code` | pipe``. |
""",
        encoding="utf-8",
    )

    assert _table(document, {"Name", "Narrative"}) == [
        {
            "Name": "`provider`",
            "Narrative": (
                "Escaped \\| pipe, `inline|code`, and ``inline `code` | pipe``."
            ),
        }
    ]


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
