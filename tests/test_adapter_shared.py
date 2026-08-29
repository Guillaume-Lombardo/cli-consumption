from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from importlib import import_module
from typing import Any

import pytest

from cli_consumption.adapters._shared import (
    MAX_BIGINT,
    add_tokens,
    basic_label,
    bounded_sum,
    counter,
    iso,
    label,
    list_value,
    mapping,
    project,
    sqlite_columns,
    timestamp,
    tokens,
)
from cli_consumption.models import empty_tokens

SHARED_ALIAS_CONTRACTS: dict[str, dict[str, Any]] = {
    "aider": {"_add_tokens": add_tokens, "_sum": bounded_sum},
    "amp": {
        "_add_tokens": add_tokens,
        "_counter": counter,
        "_label": basic_label,
        "_list": list_value,
        "_mapping": mapping,
        "_sum": bounded_sum,
    },
    "claude": {
        "_add_tokens": add_tokens,
        "_counter": counter,
        "_label": basic_label,
    },
    "continue_cli": {"_list": list_value, "_mapping": mapping, "_sum": bounded_sum},
    "copilot": {
        "_add_tokens": add_tokens,
        "_counter": counter,
        "_label": basic_label,
    },
    "crush": {
        "_add_tokens": add_tokens,
        "_columns": sqlite_columns,
        "_counter": counter,
        "_label": label,
        "_project": project,
        "_sum": bounded_sum,
    },
    "gemini": {
        "_add_tokens": add_tokens,
        "_counter": counter,
        "_label": basic_label,
        "_sum": bounded_sum,
    },
    "goose": {
        "_add_tokens": add_tokens,
        "_columns": sqlite_columns,
        "_counter": counter,
        "_label": basic_label,
        "_project": project,
        "_sum": bounded_sum,
    },
    "grok": {"_counter": counter},
    "kilo": {
        "_add_tokens": add_tokens,
        "_columns": sqlite_columns,
        "_counter": counter,
        "_label": basic_label,
        "_mapping": mapping,
        "_project": project,
        "_sum": bounded_sum,
    },
    "opencode": {
        "_add_tokens": add_tokens,
        "_columns": sqlite_columns,
        "_counter": counter,
        "_label": basic_label,
        "_mapping": mapping,
        "_project": project,
        "_sum": bounded_sum,
    },
    "openhands": {
        "_add_tokens": add_tokens,
        "_counter": counter,
        "_label": basic_label,
        "_list": list_value,
        "_mapping": mapping,
        "_sum": bounded_sum,
    },
    "pi": {
        "_add_tokens": add_tokens,
        "_counter": counter,
        "_label": basic_label,
        "_mapping": mapping,
        "_project": project,
        "_sum": bounded_sum,
    },
    "qwen": {
        "_add_tokens": add_tokens,
        "_counter": counter,
        "_label": basic_label,
        "_sum": bounded_sum,
    },
}


@pytest.mark.parametrize(("adapter", "aliases"), SHARED_ALIAS_CONTRACTS.items())
def test_adapter_private_helper_aliases_are_preserved(
    adapter: str, aliases: dict[str, Any]
) -> None:
    module = import_module(f"cli_consumption.adapters.{adapter}")
    for private_name, shared_helper in aliases.items():
        assert getattr(module, private_name) is shared_helper


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (False, 0),
        (True, 0),
        (-1, 0),
        (-1.5, 0),
        (1.9, 1),
        (float("nan"), 0),
        (float("inf"), 0),
        (-float("inf"), 0),
        (MAX_BIGINT, MAX_BIGINT),
        (MAX_BIGINT + 1, MAX_BIGINT),
        (str(MAX_BIGINT), 0),
    ],
)
def test_counter_boundaries(value: object, expected: int) -> None:
    assert counter(value) == expected


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ((), 0),
        ((1, 2, 3), 6),
        ((MAX_BIGINT - 1, 1), MAX_BIGINT),
        ((MAX_BIGINT, 1), MAX_BIGINT),
    ],
)
def test_bounded_sum_boundaries(values: tuple[int, ...], expected: int) -> None:
    assert bounded_sum(*values) == expected


@pytest.mark.parametrize(
    ("value", "maximum", "expected"),
    [
        (" model-1 ", 16, "model-1"),
        ("", 16, None),
        ("privacy canary\nsecret", 64, None),
        ("modèle", 16, None),
        ("a" * 17, 16, None),
        (True, 16, None),
    ],
)
def test_basic_label_boundaries(
    value: object, maximum: int, expected: str | None
) -> None:
    assert basic_label(value, maximum) == expected


def test_label_grammars_preserve_provider_compatibility() -> None:
    assert label("provider@model", 32) == "provider@model"
    assert basic_label("provider@model", 32) is None


def test_mapping_and_list_value_preserve_only_expected_container_types() -> None:
    source_mapping = {"secret": "discarded by adapters"}
    source_list = ["privacy canary"]

    assert mapping(source_mapping) is source_mapping
    assert mapping(source_list) == {}
    assert list_value(source_list) is source_list
    assert list_value(source_mapping) == []


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-08-29T12:34:56Z", datetime(2026, 8, 29, 12, 34, 56, tzinfo=UTC)),
        (1_777_117_696, datetime(2026, 4, 25, 11, 48, 16, tzinfo=UTC)),
        (1_777_117_696_000, datetime(2026, 4, 25, 11, 48, 16, tzinfo=UTC)),
        (True, None),
        (float("nan"), None),
        ("malformed privacy canary", None),
        ("échec", None),
    ],
)
def test_timestamp_and_iso_boundaries(value: object, expected: datetime | None) -> None:
    parsed = timestamp(value)
    assert parsed == expected
    assert iso(parsed) == (expected.isoformat() if expected else None)


def test_tokens_and_add_tokens_saturate_without_changing_composition() -> None:
    value = tokens(
        uncached=10,
        cached=3,
        cache_write=2,
        visible=7,
        reasoning=5,
        total=30,
    )
    assert value == {
        "input_tokens": 15,
        "cached_input_tokens": 3,
        "cache_write_input_tokens": 2,
        "output_tokens": 12,
        "reasoning_output_tokens": 5,
        "total_tokens": 30,
        "uncached_input_tokens": 10,
        "visible_output_tokens": 7,
        "unattributed_tokens": 3,
    }

    aggregate = empty_tokens()
    aggregate["input_tokens"] = MAX_BIGINT
    add_tokens(aggregate, value)
    assert aggregate["input_tokens"] == MAX_BIGINT
    assert aggregate["total_tokens"] == 30


@pytest.mark.parametrize(
    ("directory", "expected"),
    [
        ("/srv/work/acme/project", ("project", "mapping")),
        ("/srv/workshop/acme", ("outside-project", "none")),
        (r"C:\work\acme\project", ("windows", "mapping")),
        (None, ("outside-project", "none")),
    ],
)
def test_project_longest_prefix_and_boundaries(
    directory: str | None, expected: tuple[str, str]
) -> None:
    mappings = [
        ("work", "/srv/work"),
        ("project", "/srv/work/acme"),
        ("windows", r"C:\work\acme"),
    ]
    assert project(directory, mappings) == expected


def test_sqlite_columns_matches_provider_row_factory_contract() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        connection.execute('CREATE TABLE "messages" ("id" TEXT, "data" JSON)')
        assert sqlite_columns(connection, "messages") == {"id", "data"}
        assert sqlite_columns(connection, "missing") == set()
    finally:
        connection.close()
