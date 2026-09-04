from __future__ import annotations

import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

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
    MAX_DASHBOARD_WIDGETS,
    WIDGET_REGISTRY,
    DashboardLayoutV1,
    canonical_layout,
    load_dashboard_layout,
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
    assert resolve_dashboard_layout({"widgets": CANARY}) == DEFAULT_DASHBOARD_LAYOUT_V1


def test_sqlite_layout_round_trip_reset_and_migration(tmp_path: Path) -> None:
    engine = create_database_engine(tmp_path / "layout.sqlite")
    initialize_database(engine)
    assert load_dashboard_layout(engine) == DEFAULT_DASHBOARD_LAYOUT_V1

    custom = DEFAULT_DASHBOARD_LAYOUT_V1.model_copy(deep=True)
    custom.widgets = list(reversed(custom.widgets))
    assert save_dashboard_layout(engine, custom) == custom
    assert load_dashboard_layout(engine) == custom
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT owner_key, layout_json FROM dashboard_layouts")
        ).all()
    assert len(rows) == 1
    assert rows[0][0] == "deployment-operator"
    assert CANARY not in rows[0][1]

    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE dashboard_layouts SET layout_json=:invalid "
                "WHERE owner_key='deployment-operator'"
            ),
            {"invalid": json.dumps({"secret": CANARY})},
        )
    assert load_dashboard_layout(engine) == DEFAULT_DASHBOARD_LAYOUT_V1

    assert reset_dashboard_layout(engine) == DEFAULT_DASHBOARD_LAYOUT_V1
    assert load_dashboard_layout(engine) == DEFAULT_DASHBOARD_LAYOUT_V1
    downgrade_database(engine, "0005")
    assert "dashboard_layouts" not in inspect(engine).get_table_names()
    upgrade_database(engine)
    assert "dashboard_layouts" in inspect(engine).get_table_names()
    assert load_dashboard_layout(engine) == DEFAULT_DASHBOARD_LAYOUT_V1
    engine.dispose()


@pytest.mark.parametrize("mutation", ["id", "position", "config"])
def test_save_revalidates_mutated_models_before_touching_sql(
    tmp_path: Path,
    mutation: str,
) -> None:
    engine = create_database_engine(tmp_path / f"mutated-{mutation}.sqlite")
    initialize_database(engine)
    baseline = save_dashboard_layout(engine, DEFAULT_DASHBOARD_LAYOUT_V1)
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
        save_dashboard_layout(engine, unsafe)

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
                "INSERT INTO dashboard_layouts (owner_key, layout_json) "
                "VALUES ('deployment-operator', :layout)"
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


def test_concurrent_sqlite_layout_writes_are_atomic_last_commit_wins(
    tmp_path: Path,
) -> None:
    engine = create_database_engine(tmp_path / "concurrent-layout.sqlite")
    initialize_database(engine)
    first = DEFAULT_DASHBOARD_LAYOUT_V1.model_copy(deep=True)
    first.widgets = first.widgets[:1]
    second = DEFAULT_DASHBOARD_LAYOUT_V1.model_copy(deep=True)
    second.widgets = second.widgets[-1:]
    barrier = Barrier(3)

    def write(layout: DashboardLayoutV1) -> None:
        barrier.wait()
        save_dashboard_layout(engine, layout)

    with ThreadPoolExecutor(max_workers=2) as executor:
        writes = [executor.submit(write, layout) for layout in (first, second)]
        barrier.wait()
        for write_result in writes:
            write_result.result()

    assert load_dashboard_layout(engine) in (first, second)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT COUNT(*) FROM dashboard_layouts")) == 1
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
        saved = await client.put(
            "/api/v1/reporting/layout",
            json=custom.model_dump(mode="json"),
            headers=layout_headers,
        )
        invalid = await client.put(
            "/api/v1/reporting/layout",
            json={"version": 1, "columns": 12, "widgets": [], "secret": CANARY},
            headers=layout_headers,
        )
        invalid_identifier = await client.put(
            "/api/v1/reporting/layout",
            json=unsafe_identifier,
            headers=layout_headers,
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
        reset = await client.delete("/api/v1/reporting/layout", headers=layout_headers)

    assert missing.status_code == 401
    assert initial.json() == DEFAULT_DASHBOARD_LAYOUT_V1.model_dump(mode="json")
    assert saved.json() == custom.model_dump(mode="json")
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

        first = DEFAULT_DASHBOARD_LAYOUT_V1.model_copy(deep=True)
        first.widgets = first.widgets[:2]
        second = DEFAULT_DASHBOARD_LAYOUT_V1.model_copy(deep=True)
        second.widgets = list(reversed(second.widgets))
        save_dashboard_layout(scoped_engine, first)
        save_dashboard_layout(scoped_engine, second)
        assert load_dashboard_layout(scoped_engine) == second
        with scoped_engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT COUNT(*) FROM dashboard_layouts")) == 1
            )
        assert reset_dashboard_layout(scoped_engine) == DEFAULT_DASHBOARD_LAYOUT_V1
        assert load_dashboard_layout(scoped_engine) == DEFAULT_DASHBOARD_LAYOUT_V1
    finally:
        if scoped_engine is not None:
            scoped_engine.dispose()
        if schema_created:
            with admin_engine.begin() as connection:
                connection.execute(DropSchema(schema_name, cascade=True))
        admin_engine.dispose()
