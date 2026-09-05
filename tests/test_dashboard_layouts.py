from __future__ import annotations

import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Any, cast

import httpx
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema, DropSchema

from cli_consumption.api import create_app
from cli_consumption.dashboard import generate_dashboard
from cli_consumption.dashboard_layouts import (
    DASHBOARD_GRID_COLUMNS,
    DASHBOARD_GRID_ROWS,
    DASHBOARD_LAYOUT_VERSION,
    DEFAULT_DASHBOARD_LAYOUT_V1,
    MAX_DASHBOARD_LAYOUT_BYTES,
    MAX_DASHBOARD_LAYOUT_REVISION,
    MAX_DASHBOARD_WIDGETS,
    WIDGET_REGISTRY,
    DashboardLayoutConflictError,
    DashboardLayoutV1,
    canonical_layout,
    dashboard_layout_revision,
    load_dashboard_layout,
    load_dashboard_layout_state,
    reset_dashboard_layout,
    resolve_dashboard_layout,
    save_dashboard_layout,
)
from cli_consumption.reporting_api import DashboardQuery, ReportingRuntime
from cli_consumption.schema import downgrade_database, upgrade_database
from cli_consumption.storage import create_database_engine, initialize_database

CANARY = "PRIVATE-LAYOUT-CANARY-DO-NOT-EXPOSE"
ID_CANARY = "activity-private-project-label"
READ_VALUE = "read-value"
LAYOUT_VALUE = "layout-value"
CONTRACT_FIXTURE = (
    Path(__file__).parent / "fixtures" / "dashboard_layout_v1_contract.json"
)


def test_python_contract_matches_the_serialized_cross_runtime_definition() -> None:
    fixture = json.loads(CONTRACT_FIXTURE.read_text(encoding="utf-8"))
    assert fixture["constraints"] == {
        "version": DASHBOARD_LAYOUT_VERSION,
        "columns": DASHBOARD_GRID_COLUMNS,
        "rows": DASHBOARD_GRID_ROWS,
        "maxWidgets": MAX_DASHBOARD_WIDGETS,
        "maxBytes": MAX_DASHBOARD_LAYOUT_BYTES,
        "instanceSuffixMin": 1,
        "instanceSuffixMax": MAX_DASHBOARD_WIDGETS,
    }
    assert fixture["registry"] == {
        widget_type: {
            "minWidth": limits[0],
            "maxWidth": limits[1],
            "minHeight": limits[2],
            "maxHeight": limits[3],
        }
        for widget_type, limits in WIDGET_REGISTRY.items()
    }
    assert fixture["default"] == DEFAULT_DASHBOARD_LAYOUT_V1.model_dump(mode="json")
    assert canonical_layout(DEFAULT_DASHBOARD_LAYOUT_V1) == json.dumps(
        fixture["default"],
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    for suffix in (
        fixture["constraints"]["instanceSuffixMin"],
        fixture["constraints"]["instanceSuffixMax"],
    ):
        document = DEFAULT_DASHBOARD_LAYOUT_V1.model_dump(mode="json")
        document["widgets"] = [document["widgets"][1]]
        document["widgets"][0]["id"] = f"activity-{suffix}"
        DashboardLayoutV1.model_validate(document)


def test_default_layout_is_the_deterministic_legacy_composition() -> None:
    layout = DEFAULT_DASHBOARD_LAYOUT_V1.model_dump(mode="json")

    assert layout["version"] == 1
    assert layout["columns"] == 12
    assert [widget["type"] for widget in layout["widgets"]] == [
        "headline-metrics",
        "activity",
        "tools",
        "models",
        "turn-performance",
        "workflow-complexity",
        "turn-outcomes",
        "context-pressure",
        "technical-work-items",
        "cohorts",
        "data-quality",
        "conversation-explorer",
    ]
    assert CANARY not in json.dumps(layout)


def test_widget_instance_identifiers_are_structural_and_bounded() -> None:
    document = DEFAULT_DASHBOARD_LAYOUT_V1.model_dump(mode="json")
    activity = document["widgets"][1]
    document["widgets"] = [
        activity,
        {
            **activity,
            "id": "activity-2",
            "position": {"x": 6, "y": 1},
        },
    ]

    assert DashboardLayoutV1.model_validate(document).widgets[1].id == "activity-2"


@pytest.mark.parametrize(
    "mutation",
    [
        {"version": 2},
        {"columns": 13},
        {"unexpected": CANARY},
        {
            "widgets": [
                {
                    "id": ID_CANARY,
                    "type": "activity",
                    "position": {"x": 0, "y": 0},
                    "size": {"width": 6, "height": 1},
                    "config": {},
                }
            ]
        },
        {
            "widgets": [
                {
                    "id": "activity-01",
                    "type": "activity",
                    "position": {"x": 0, "y": 0},
                    "size": {"width": 6, "height": 1},
                    "config": {},
                }
            ]
        },
        {
            "widgets": [
                {
                    "id": "activity-33",
                    "type": "activity",
                    "position": {"x": 0, "y": 0},
                    "size": {"width": 6, "height": 1},
                    "config": {},
                }
            ]
        },
        {"widgets": []},
        {
            "widgets": [
                {
                    "id": "activity",
                    "type": "activity",
                    "position": {"x": 10, "y": 0},
                    "size": {"width": 6, "height": 1},
                    "config": {},
                }
            ]
        },
        {
            "widgets": [
                {
                    "id": "activity",
                    "type": "activity",
                    "position": {"x": 0, "y": 10**100},
                    "size": {"width": 6, "height": 1},
                    "config": {},
                }
            ]
        },
        {
            "widgets": [
                {
                    "id": "activity",
                    "type": "activity",
                    "position": {"x": 0, "y": 0},
                    "size": {"width": 6, "height": 1},
                    "config": {},
                },
                {
                    "id": "tools",
                    "type": "tools",
                    "position": {"x": 0, "y": 0},
                    "size": {"width": 6, "height": 1},
                    "config": {},
                },
            ]
        },
        {
            "widgets": [
                {
                    "id": "activity",
                    "type": "activity",
                    "position": {"x": 0, "y": 0},
                    "size": {"width": 6, "height": 1},
                    "config": {"prompt": CANARY},
                }
            ]
        },
    ],
)
def test_layout_validation_rejects_adversarial_documents(
    mutation: dict[str, object],
) -> None:
    candidate = DEFAULT_DASHBOARD_LAYOUT_V1.model_dump(mode="json")
    candidate.update(mutation)
    with pytest.raises(ValueError):
        DashboardLayoutV1.model_validate(candidate)


def test_retired_widget_is_dropped_and_corruption_resets_to_default() -> None:
    stored = DEFAULT_DASHBOARD_LAYOUT_V1.model_dump(mode="json")
    stored["widgets"].insert(
        1,
        {
            "id": "retired",
            "type": "retired-widget",
            "position": {"x": 0, "y": 1},
            "size": {"width": 6, "height": 1},
            "config": {},
        },
    )
    assert resolve_dashboard_layout(stored) == DEFAULT_DASHBOARD_LAYOUT_V1
    mixed = {
        "version": 1,
        "columns": 12,
        "widgets": [
            DEFAULT_DASHBOARD_LAYOUT_V1.widgets[1].model_dump(mode="json"),
            {
                "id": CANARY,
                "type": "retired-widget",
                "position": {"x": 6, "y": 1},
                "size": {"width": 6, "height": 1},
                "config": {},
            },
        ],
    }
    resolved = resolve_dashboard_layout(mixed)
    assert resolved.widgets == [DEFAULT_DASHBOARD_LAYOUT_V1.widgets[1]]
    assert CANARY not in resolved.model_dump_json()
    all_retired = {**mixed, "widgets": [mixed["widgets"][1]]}
    assert resolve_dashboard_layout(all_retired) == DEFAULT_DASHBOARD_LAYOUT_V1
    assert resolve_dashboard_layout({"widgets": CANARY}) == DEFAULT_DASHBOARD_LAYOUT_V1


def test_sqlite_layout_round_trip_reset_and_migration(tmp_path: Path) -> None:
    engine = create_database_engine(tmp_path / "layout.sqlite")
    initialize_database(engine)
    assert load_dashboard_layout(engine) == DEFAULT_DASHBOARD_LAYOUT_V1

    custom = DEFAULT_DASHBOARD_LAYOUT_V1.model_copy(deep=True)
    custom.widgets = list(reversed(custom.widgets))
    saved = save_dashboard_layout(engine, custom, expected_revision=0)
    assert saved.layout == custom
    assert saved.revision == 1
    assert dashboard_layout_revision(saved.etag) == 1
    assert load_dashboard_layout(engine) == custom
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT owner_key, layout_json, revision FROM dashboard_layouts")
        ).all()
    assert len(rows) == 1
    assert rows[0][0] == "deployment-operator"
    assert CANARY not in rows[0][1]
    assert rows[0][2] == 1

    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE dashboard_layouts SET layout_json=:invalid "
                "WHERE owner_key='deployment-operator'"
            ),
            {"invalid": json.dumps({"secret": CANARY})},
        )
    assert load_dashboard_layout(engine) == DEFAULT_DASHBOARD_LAYOUT_V1

    reset = reset_dashboard_layout(engine, expected_revision=1)
    assert reset.layout == DEFAULT_DASHBOARD_LAYOUT_V1
    assert reset.revision == 2
    assert load_dashboard_layout(engine) == DEFAULT_DASHBOARD_LAYOUT_V1
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT revision FROM dashboard_layouts")) == 2
    downgrade_database(engine, "0005")
    assert "dashboard_layouts" not in inspect(engine).get_table_names()
    upgrade_database(engine)
    assert "dashboard_layouts" in inspect(engine).get_table_names()
    assert load_dashboard_layout(engine) == DEFAULT_DASHBOARD_LAYOUT_V1
    engine.dispose()


@pytest.mark.parametrize(
    "etag",
    [
        "",
        "0",
        'W/"AAAAAAAAAAAAAAAAAAAAAA"',
        '"AAAAAAAAAAAAAAAAAAAAA+"',
        '"AAAAAAAAAAAAAAAAAAAAAA", "AAAAAAAAAAAAAAAAAAAAAA"',
        '"AAAAAAAAAAAAAAAAAAAAAA"',
    ],
)
def test_layout_etag_decoder_rejects_unminted_or_malformed_values(etag: str) -> None:
    with pytest.raises(ValueError, match=r"^invalid_dashboard_layout_revision$"):
        dashboard_layout_revision(etag)


def test_revision_0007_upgrades_legacy_layout_and_downgrades_cleanly(
    tmp_path: Path,
) -> None:
    engine = create_database_engine(tmp_path / "layout-revision.sqlite")
    initialize_database(engine)
    downgrade_database(engine, "0006")
    legacy = canonical_layout(DEFAULT_DASHBOARD_LAYOUT_V1)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO dashboard_layouts (owner_key, layout_json) "
                "VALUES ('deployment-operator', :layout)"
            ),
            {"layout": legacy},
        )

    upgrade_database(engine)
    state = load_dashboard_layout_state(engine)
    assert state.layout == DEFAULT_DASHBOARD_LAYOUT_V1
    assert state.revision == 1
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0007"
        )

    downgrade_database(engine, "0006")
    assert {
        column["name"] for column in inspect(engine).get_columns("dashboard_layouts")
    } == {
        "owner_key",
        "layout_json",
    }
    with engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT layout_json FROM dashboard_layouts"))
            == legacy
        )
    engine.dispose()


def test_unversioned_revision_0006_layout_is_adopted_before_upgrade(
    tmp_path: Path,
) -> None:
    engine = create_database_engine(tmp_path / "unversioned-layout.sqlite")
    initialize_database(engine)
    downgrade_database(engine, "0006")
    legacy = canonical_layout(DEFAULT_DASHBOARD_LAYOUT_V1)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO dashboard_layouts (owner_key, layout_json) "
                "VALUES ('deployment-operator', :layout)"
            ),
            {"layout": legacy},
        )
        connection.execute(text("DROP TABLE alembic_version"))

    initialize_database(engine)
    assert load_dashboard_layout_state(engine).revision == 1
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0007"
        )
    engine.dispose()


def test_sequential_update_and_reset_reject_stale_revisions(tmp_path: Path) -> None:
    engine = create_database_engine(tmp_path / "layout-stale.sqlite")
    initialize_database(engine)
    first = DEFAULT_DASHBOARD_LAYOUT_V1.model_copy(deep=True)
    first.widgets = first.widgets[:2]
    second = DEFAULT_DASHBOARD_LAYOUT_V1.model_copy(deep=True)
    second.widgets = second.widgets[-1:]

    created = save_dashboard_layout(engine, first, expected_revision=0)
    updated = save_dashboard_layout(engine, second, expected_revision=created.revision)
    with pytest.raises(DashboardLayoutConflictError, match=r"^layout_conflict$"):
        save_dashboard_layout(engine, first, expected_revision=created.revision)
    with pytest.raises(DashboardLayoutConflictError, match=r"^layout_conflict$"):
        reset_dashboard_layout(engine, expected_revision=created.revision)
    reset = reset_dashboard_layout(engine, expected_revision=updated.revision)
    with pytest.raises(DashboardLayoutConflictError, match=r"^layout_conflict$"):
        save_dashboard_layout(engine, first, expected_revision=updated.revision)

    assert reset.revision == 3
    assert load_dashboard_layout_state(engine) == reset
    engine.dispose()


def test_layout_revision_is_non_null_bounded_and_never_overflows(
    tmp_path: Path,
) -> None:
    engine = create_database_engine(tmp_path / "layout-max-revision.sqlite")
    initialize_database(engine)
    save_dashboard_layout(engine, DEFAULT_DASHBOARD_LAYOUT_V1, expected_revision=0)
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE dashboard_layouts SET revision=:maximum"),
            {"maximum": MAX_DASHBOARD_LAYOUT_REVISION},
        )
    with pytest.raises(DashboardLayoutConflictError, match=r"^layout_conflict$"):
        reset_dashboard_layout(engine, expected_revision=MAX_DASHBOARD_LAYOUT_REVISION)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT revision FROM dashboard_layouts")) == (
            MAX_DASHBOARD_LAYOUT_REVISION
        )
    engine.dispose()


@pytest.mark.parametrize("mutation", ["id", "position", "config"])
def test_save_revalidates_mutated_models_before_touching_sql(
    tmp_path: Path,
    mutation: str,
) -> None:
    engine = create_database_engine(tmp_path / f"mutated-{mutation}.sqlite")
    initialize_database(engine)
    baseline = save_dashboard_layout(
        engine, DEFAULT_DASHBOARD_LAYOUT_V1, expected_revision=0
    ).layout
    with engine.connect() as connection:
        stored_before = connection.scalar(
            text("SELECT layout_json FROM dashboard_layouts")
        )

    unsafe = baseline.model_copy(deep=True)
    if mutation == "id":
        unsafe.widgets[1].id = ID_CANARY
    elif mutation == "position":
        unsafe.widgets[1].position.x = 12
    else:
        unsafe.widgets[1].config["prompt"] = CANARY

    with pytest.raises(ValueError, match=r"^invalid_dashboard_layout$"):
        canonical_layout(unsafe)
    with pytest.raises(ValueError, match=r"^invalid_dashboard_layout$"):
        save_dashboard_layout(engine, unsafe, expected_revision=1)

    with engine.connect() as connection:
        stored_after = connection.scalar(
            text("SELECT layout_json FROM dashboard_layouts")
        )
    assert stored_after == stored_before
    assert ID_CANARY not in str(stored_after)
    assert CANARY not in str(stored_after)
    engine.dispose()


@pytest.mark.parametrize("share_safe", [False, True], ids=["detailed", "share-safe"])
def test_dashboard_export_revalidates_a_mutated_layout_before_writing(
    tmp_path: Path,
    share_safe: bool,
) -> None:
    engine = create_database_engine(tmp_path / f"export-{share_safe}.sqlite")
    unsafe = DEFAULT_DASHBOARD_LAYOUT_V1.model_copy(deep=True)
    unsafe.widgets[1].id = ID_CANARY
    output = tmp_path / f"export-{share_safe}.html"

    with pytest.raises(ValueError, match=r"^invalid_dashboard_layout$") as caught:
        generate_dashboard(engine, output, share_safe=share_safe, layout=unsafe)

    assert ID_CANARY not in str(caught.value)
    assert CANARY not in str(caught.value)
    assert not output.exists()
    engine.dispose()


def test_dashboard_export_rejects_an_untrusted_theme_before_writing(
    tmp_path: Path,
) -> None:
    engine = create_database_engine(tmp_path / "unsafe-theme.sqlite")
    output = tmp_path / "unsafe-theme.html"

    with pytest.raises(ValueError, match=r"^invalid_dashboard_theme$"):
        generate_dashboard(engine, output, theme=cast(Any, CANARY))

    assert not output.exists()
    engine.dispose()


@pytest.mark.parametrize("profile", ["detailed", "share-safe"])
def test_free_form_identifier_from_storage_never_reaches_offline_html(
    tmp_path: Path,
    profile: str,
) -> None:
    engine = create_database_engine(tmp_path / f"layout-{profile}.sqlite")
    initialize_database(engine)
    unsafe = DEFAULT_DASHBOARD_LAYOUT_V1.model_dump(mode="json")
    unsafe["widgets"][1]["id"] = ID_CANARY
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO dashboard_layouts (owner_key, layout_json, revision) "
                "VALUES ('deployment-operator', :layout, 1)"
            ),
            {"layout": json.dumps(unsafe)},
        )

    assert load_dashboard_layout(engine) == DEFAULT_DASHBOARD_LAYOUT_V1
    query = DashboardQuery.model_validate(
        {
            "version": 1,
            "window": {"since": None, "until": None},
            "filters": {
                "providers": [],
                "machines": [],
                "projects": [],
                "models": [],
            },
            "profile": profile,
        }
    )
    output = ReportingRuntime(engine).export(query)
    try:
        html = output.read_text(encoding="utf-8")
        assert ID_CANARY not in html
        assert CANARY not in html
        assert '"id":"activity"' in html
    finally:
        output.unlink()
        engine.dispose()


def test_concurrent_sqlite_layout_writes_allow_exactly_one_cas_winner(
    tmp_path: Path,
) -> None:
    engine = create_database_engine(tmp_path / "concurrent-layout.sqlite")
    initialize_database(engine)
    first = DEFAULT_DASHBOARD_LAYOUT_V1.model_copy(deep=True)
    first.widgets = first.widgets[:1]
    second = DEFAULT_DASHBOARD_LAYOUT_V1.model_copy(deep=True)
    second.widgets = second.widgets[-1:]
    barrier = Barrier(3)

    def write(layout: DashboardLayoutV1) -> str:
        barrier.wait()
        try:
            save_dashboard_layout(engine, layout, expected_revision=0)
        except DashboardLayoutConflictError:
            return "conflict"
        return "saved"

    with ThreadPoolExecutor(max_workers=2) as executor:
        writes = [executor.submit(write, layout) for layout in (first, second)]
        barrier.wait()
        assert sorted(write_result.result() for write_result in writes) == [
            "conflict",
            "saved",
        ]

    assert load_dashboard_layout(engine) in (first, second)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT COUNT(*) FROM dashboard_layouts")) == 1
    engine.dispose()


@pytest.mark.parametrize(
    ("left_operation", "right_operation"),
    [
        ("update-first", "update-second"),
        ("update-first", "reset"),
        ("reset", "update-second"),
    ],
)
def test_concurrent_sqlite_layout_mutations_allow_one_revision_winner(
    tmp_path: Path,
    left_operation: str,
    right_operation: str,
) -> None:
    engine = create_database_engine(
        tmp_path / f"concurrent-{left_operation}-{right_operation}.sqlite"
    )
    initialize_database(engine)
    save_dashboard_layout(engine, DEFAULT_DASHBOARD_LAYOUT_V1, expected_revision=0)
    first = DEFAULT_DASHBOARD_LAYOUT_V1.model_copy(deep=True)
    first.widgets = first.widgets[:1]
    second = DEFAULT_DASHBOARD_LAYOUT_V1.model_copy(deep=True)
    second.widgets = second.widgets[-1:]
    barrier = Barrier(3)

    def mutate(operation: str) -> str:
        barrier.wait()
        try:
            if operation == "reset":
                reset_dashboard_layout(engine, expected_revision=1)
            else:
                layout = first if operation == "update-first" else second
                save_dashboard_layout(engine, layout, expected_revision=1)
        except DashboardLayoutConflictError:
            return "conflict"
        return "saved"

    with ThreadPoolExecutor(max_workers=2) as executor:
        mutations = [
            executor.submit(mutate, operation)
            for operation in (left_operation, right_operation)
        ]
        barrier.wait()
        assert sorted(result.result() for result in mutations) == [
            "conflict",
            "saved",
        ]

    assert load_dashboard_layout_state(engine).revision == 2
    engine.dispose()


@pytest.mark.anyio
async def test_layout_api_round_trip_reset_and_privacy(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    engine = create_database_engine(tmp_path / "layout-api.sqlite")
    app = create_app(
        engine,
        "ingest-token",
        read_token=READ_VALUE,
        layout_token=LAYOUT_VALUE,
    )
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {READ_VALUE}"}
    layout_headers = {"Authorization": f"Bearer {LAYOUT_VALUE}"}
    custom = DEFAULT_DASHBOARD_LAYOUT_V1.model_copy(deep=True)
    custom.widgets = list(reversed(custom.widgets))
    unsafe_identifier = custom.model_dump(mode="json")
    unsafe_identifier["widgets"][0]["id"] = ID_CANARY
    async with httpx.AsyncClient(
        transport=transport, base_url="http://collector.test"
    ) as client:
        missing = await client.get("/api/v1/reporting/layout")
        initial = await client.get("/api/v1/reporting/layout", headers=headers)
        assert dashboard_layout_revision(initial.headers["etag"]) == 0
        saved = await client.put(
            "/api/v1/reporting/layout",
            json=custom.model_dump(mode="json"),
            headers={**layout_headers, "If-Match": initial.headers["etag"]},
        )
        saved_etag = saved.headers["etag"]
        missing_revision = await client.put(
            "/api/v1/reporting/layout",
            json=custom.model_dump(mode="json"),
            headers=layout_headers,
        )
        malformed_revision = await client.delete(
            "/api/v1/reporting/layout",
            headers={**layout_headers, "If-Match": CANARY},
        )
        stale = await client.put(
            "/api/v1/reporting/layout",
            json=custom.model_dump(mode="json"),
            headers={**layout_headers, "If-Match": initial.headers["etag"]},
        )
        invalid = await client.put(
            "/api/v1/reporting/layout",
            json={"version": 1, "columns": 12, "widgets": [], "secret": CANARY},
            headers={**layout_headers, "If-Match": saved_etag},
        )
        invalid_identifier = await client.put(
            "/api/v1/reporting/layout",
            json=unsafe_identifier,
            headers={**layout_headers, "If-Match": saved_etag},
        )
        with engine.connect() as connection:
            stored_layout = connection.scalar(
                text(
                    "SELECT layout_json FROM dashboard_layouts "
                    "WHERE owner_key='deployment-operator'"
                )
            )
        assert stored_layout is not None
        assert ID_CANARY not in stored_layout
        denied = await client.delete("/api/v1/reporting/layout", headers=headers)
        denied_put = await client.put(
            "/api/v1/reporting/layout",
            json=custom.model_dump(mode="json"),
            headers=headers,
        )
        layout_cannot_read = await client.get(
            "/api/v1/reporting/layout", headers=layout_headers
        )
        layout_cannot_export = await client.post(
            "/api/v1/reporting/export",
            json={
                "version": 1,
                "window": {"since": None, "until": None},
                "filters": {
                    "providers": [],
                    "machines": [],
                    "projects": [],
                    "models": [],
                },
                "profile": "detailed",
            },
            headers=layout_headers,
        )
        layout_cannot_ingest = await client.post(
            "/api/v1/snapshots", json={"provider": "codex"}, headers=layout_headers
        )
        reset = await client.delete(
            "/api/v1/reporting/layout",
            headers={**layout_headers, "If-Match": saved_etag},
        )

    assert missing.status_code == 401
    assert initial.json() == DEFAULT_DASHBOARD_LAYOUT_V1.model_dump(mode="json")
    assert saved.json() == custom.model_dump(mode="json")
    assert dashboard_layout_revision(saved.headers["etag"]) == 1
    assert missing_revision.status_code == 428
    assert missing_revision.json() == {"detail": "layout_revision_required"}
    assert malformed_revision.status_code == 400
    assert malformed_revision.json() == {"detail": "invalid_layout_revision"}
    assert stale.status_code == 412
    assert stale.json() == {"detail": "layout_conflict"}
    assert invalid.status_code == 422
    assert invalid.json() == {"detail": "invalid_reporting_request"}
    assert invalid_identifier.status_code == 422
    assert invalid_identifier.json() == {"detail": "invalid_reporting_request"}
    assert denied.status_code == 403
    assert denied_put.status_code == 403
    assert layout_cannot_read.status_code == 403
    assert layout_cannot_export.status_code == 403
    assert layout_cannot_ingest.status_code == 403
    assert CANARY not in invalid.text
    assert ID_CANARY not in invalid_identifier.text
    assert ID_CANARY not in caplog.text
    assert reset.json() == DEFAULT_DASHBOARD_LAYOUT_V1.model_dump(mode="json")
    assert dashboard_layout_revision(reset.headers["etag"]) == 2
    engine.dispose()


def test_postgresql_layout_upsert_and_reset_when_configured() -> None:
    database_url = os.environ.get("TEST_POSTGRESQL_URL")
    if database_url is None:
        pytest.skip("TEST_POSTGRESQL_URL is not configured")

    schema_name = f"layout_test_{uuid.uuid4().hex}"
    admin_engine = create_engine(database_url)
    scoped_engine = None
    schema_created = False
    try:
        with admin_engine.begin() as connection:
            connection.execute(CreateSchema(schema_name))
        schema_created = True
        scoped_url = make_url(database_url).update_query_dict(
            {"options": f"-csearch_path={schema_name}"}
        )
        scoped_engine = create_engine(scoped_url)
        initialize_database(scoped_engine)
        downgrade_database(scoped_engine, "0006")
        legacy = canonical_layout(DEFAULT_DASHBOARD_LAYOUT_V1)
        with scoped_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO dashboard_layouts (owner_key, layout_json) "
                    "VALUES ('deployment-operator', :layout)"
                ),
                {"layout": legacy},
            )
        upgrade_database(scoped_engine)
        assert load_dashboard_layout_state(scoped_engine).revision == 1

        first = DEFAULT_DASHBOARD_LAYOUT_V1.model_copy(deep=True)
        first.widgets = first.widgets[:2]
        second = DEFAULT_DASHBOARD_LAYOUT_V1.model_copy(deep=True)
        second.widgets = list(reversed(second.widgets))
        save_dashboard_layout(scoped_engine, first, expected_revision=1)
        save_dashboard_layout(scoped_engine, second, expected_revision=2)
        assert load_dashboard_layout(scoped_engine) == second
        with scoped_engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT COUNT(*) FROM dashboard_layouts")) == 1
            )
        assert (
            reset_dashboard_layout(scoped_engine, expected_revision=3).layout
            == DEFAULT_DASHBOARD_LAYOUT_V1
        )
        assert load_dashboard_layout(scoped_engine) == DEFAULT_DASHBOARD_LAYOUT_V1
        barrier = Barrier(3)

        def concurrent_write(layout: DashboardLayoutV1) -> str:
            barrier.wait()
            try:
                save_dashboard_layout(scoped_engine, layout, expected_revision=4)
            except DashboardLayoutConflictError:
                return "conflict"
            return "saved"

        with ThreadPoolExecutor(max_workers=2) as executor:
            writes = [
                executor.submit(concurrent_write, item) for item in (first, second)
            ]
            barrier.wait()
            assert sorted(result.result() for result in writes) == ["conflict", "saved"]
        state = load_dashboard_layout_state(scoped_engine)
        assert state.revision == 5
        with scoped_engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE dashboard_layouts SET revision=:maximum "
                    "WHERE owner_key='deployment-operator'"
                ),
                {"maximum": MAX_DASHBOARD_LAYOUT_REVISION},
            )
        with pytest.raises(DashboardLayoutConflictError, match=r"^layout_conflict$"):
            reset_dashboard_layout(
                scoped_engine, expected_revision=MAX_DASHBOARD_LAYOUT_REVISION
            )
    finally:
        if scoped_engine is not None:
            scoped_engine.dispose()
        if schema_created:
            with admin_engine.begin() as connection:
                connection.execute(DropSchema(schema_name, cascade=True))
        admin_engine.dispose()
