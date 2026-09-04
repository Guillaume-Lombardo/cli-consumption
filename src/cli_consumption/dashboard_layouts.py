"""Versioned, content-free dashboard layout validation and persistence."""

from __future__ import annotations

import json
from typing import Any, Literal, Self, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)
from sqlalchemy import Table, case, delete, func, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine

from cli_consumption.storage import DashboardLayout

DASHBOARD_LAYOUT_VERSION = 1
DASHBOARD_GRID_COLUMNS = 12
DASHBOARD_GRID_ROWS = 64
MAX_DASHBOARD_WIDGETS = 32
MAX_DASHBOARD_LAYOUT_BYTES = 64 * 1024
_OWNER_KEY = "deployment-operator"
_INSTANCE_SUFFIXES = frozenset(
    str(index) for index in range(1, MAX_DASHBOARD_WIDGETS + 1)
)

WIDGET_REGISTRY: dict[str, tuple[int, int, int, int]] = {
    "headline-metrics": (12, 12, 1, 2),
    "activity": (3, 12, 1, 4),
    "tools": (3, 12, 1, 4),
    "models": (3, 12, 1, 4),
    "turn-performance": (3, 12, 1, 4),
    "workflow-complexity": (3, 12, 1, 4),
    "turn-outcomes": (3, 12, 1, 4),
    "context-pressure": (3, 12, 1, 4),
    "technical-work-items": (3, 12, 1, 4),
    "cohorts": (6, 12, 1, 6),
    "data-quality": (3, 12, 1, 4),
    "conversation-explorer": (12, 12, 2, 8),
}
WidgetType = Literal[
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


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class GridPosition(_StrictModel):
    x: StrictInt = Field(ge=0)
    y: StrictInt = Field(ge=0)


class WidgetSize(_StrictModel):
    width: StrictInt = Field(ge=1)
    height: StrictInt = Field(ge=1)


class DashboardWidgetV1(_StrictModel):
    id: StrictStr = Field(min_length=1, max_length=64)
    type: WidgetType
    position: GridPosition
    size: WidgetSize
    config: dict[str, Any]

    @model_validator(mode="after")
    def structural_identifier(self) -> Self:
        prefix = f"{self.type}-"
        if self.id != self.type and not (
            self.id.startswith(prefix)
            and self.id.removeprefix(prefix) in _INSTANCE_SUFFIXES
        ):
            raise ValueError("invalid dashboard widget identifier")
        return self

    @field_validator("config")
    @classmethod
    def empty_configuration(cls, value: dict[str, Any]) -> dict[str, Any]:
        if value:
            raise ValueError("unknown dashboard widget configuration")
        return value


class DashboardLayoutV1(_StrictModel):
    version: Literal[1]
    columns: Literal[12]
    widgets: list[DashboardWidgetV1] = Field(
        min_length=1, max_length=MAX_DASHBOARD_WIDGETS
    )

    @field_validator("widgets")
    @classmethod
    def validate_widgets(
        cls, widgets: list[DashboardWidgetV1]
    ) -> list[DashboardWidgetV1]:
        if len({widget.id for widget in widgets}) != len(widgets):
            raise ValueError("duplicate dashboard widget identifier")
        for widget in widgets:
            min_width, max_width, min_height, max_height = WIDGET_REGISTRY[widget.type]
            if (
                not min_width <= widget.size.width <= max_width
                or not min_height <= widget.size.height <= max_height
                or widget.position.x + widget.size.width > DASHBOARD_GRID_COLUMNS
                or widget.position.y + widget.size.height > DASHBOARD_GRID_ROWS
            ):
                raise ValueError("dashboard widget is outside the grid")
        for index, left in enumerate(widgets):
            for right in widgets[index + 1 :]:
                if (
                    left.position.x < right.position.x + right.size.width
                    and left.position.x + left.size.width > right.position.x
                    and left.position.y < right.position.y + right.size.height
                    and left.position.y + left.size.height > right.position.y
                ):
                    raise ValueError("dashboard widgets overlap")
        return widgets


def _default_widget(widget_type: str, index: int) -> dict[str, Any]:
    full = widget_type in {"headline-metrics", "conversation-explorer"}
    return {
        "id": widget_type,
        "type": widget_type,
        "position": {
            "x": 0 if full or index % 2 else 6,
            "y": 0 if index == 0 else (index + 1) // 2,
        },
        "size": {
            "width": 12 if full else 6,
            "height": 2 if widget_type == "conversation-explorer" else 1,
        },
        "config": {},
    }


DEFAULT_DASHBOARD_LAYOUT_V1 = DashboardLayoutV1.model_validate(
    {
        "version": 1,
        "columns": 12,
        "widgets": [
            _default_widget(widget_type, index)
            for index, widget_type in enumerate(WIDGET_REGISTRY)
        ],
    }
)


def revalidate_dashboard_layout(layout: DashboardLayoutV1) -> DashboardLayoutV1:
    """Return a detached strict copy, rejecting mutations made after construction."""
    try:
        return DashboardLayoutV1.model_validate(
            layout.model_dump(mode="python", warnings="none")
        )
    except (AttributeError, TypeError, ValueError):
        raise ValueError("invalid_dashboard_layout") from None


def _encode_validated_layout(layout: DashboardLayoutV1) -> str:
    encoded = json.dumps(
        layout.model_dump(mode="json"),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(encoded.encode("utf-8")) > MAX_DASHBOARD_LAYOUT_BYTES:
        raise ValueError("invalid_dashboard_layout")
    return encoded


def canonical_layout(layout: DashboardLayoutV1) -> str:
    return _encode_validated_layout(revalidate_dashboard_layout(layout))


def resolve_dashboard_layout(value: object) -> DashboardLayoutV1:
    """Tolerate only registered-widget retirement; otherwise return the default."""
    if not isinstance(value, dict) or not isinstance(value.get("widgets"), list):
        return DEFAULT_DASHBOARD_LAYOUT_V1.model_copy(deep=True)
    candidate = dict(value)
    candidate["widgets"] = [
        widget
        for widget in value["widgets"]
        if isinstance(widget, dict) and widget.get("type") in WIDGET_REGISTRY
    ]
    try:
        return DashboardLayoutV1.model_validate(candidate)
    except (ValueError, TypeError):
        return DEFAULT_DASHBOARD_LAYOUT_V1.model_copy(deep=True)


def load_dashboard_layout(engine: Engine) -> DashboardLayoutV1:
    with engine.connect() as connection:
        raw = connection.scalar(
            select(
                case(
                    (
                        func.length(DashboardLayout.layout_json)
                        <= MAX_DASHBOARD_LAYOUT_BYTES,
                        DashboardLayout.layout_json,
                    ),
                    else_=None,
                )
            ).where(DashboardLayout.owner_key == _OWNER_KEY)
        )
    if raw is None or len(raw.encode("utf-8")) > MAX_DASHBOARD_LAYOUT_BYTES:
        return DEFAULT_DASHBOARD_LAYOUT_V1.model_copy(deep=True)
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return DEFAULT_DASHBOARD_LAYOUT_V1.model_copy(deep=True)
    return resolve_dashboard_layout(value)


def save_dashboard_layout(
    engine: Engine, layout: DashboardLayoutV1
) -> DashboardLayoutV1:
    validated = revalidate_dashboard_layout(layout)
    raw = _encode_validated_layout(validated)
    table = cast(Table, DashboardLayout.__table__)
    insert = (
        sqlite_insert(table)
        if engine.dialect.name == "sqlite"
        else postgresql_insert(table)
    )
    statement = insert.values(owner_key=_OWNER_KEY, layout_json=raw)
    statement = statement.on_conflict_do_update(
        index_elements=[table.c.owner_key], set_={"layout_json": raw}
    )
    with engine.begin() as connection:
        connection.execute(statement)
    return validated


def reset_dashboard_layout(engine: Engine) -> DashboardLayoutV1:
    with engine.begin() as connection:
        connection.execute(
            delete(DashboardLayout).where(DashboardLayout.owner_key == _OWNER_KEY)
        )
    return DEFAULT_DASHBOARD_LAYOUT_V1.model_copy(deep=True)
