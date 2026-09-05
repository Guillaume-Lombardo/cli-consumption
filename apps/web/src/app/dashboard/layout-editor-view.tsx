import {
  DASHBOARD_GRID_COLUMNS,
  DASHBOARD_WIDGET_REGISTRY,
  type DashboardLayoutV1,
  type DashboardWidgetType,
  type DashboardWidgetV1,
} from "@cli-consumption/contracts";
import { useRef, type KeyboardEvent, type PointerEvent } from "react";

import {
  pointerGridDelta,
  type PointerGridGeometry,
  WIDGET_CATALOG,
} from "./layout-editor";

function pixelTracks(value: string): number[] {
  return [...value.matchAll(/([0-9]+(?:\.[0-9]+)?)px/g)].map((match) =>
    Number(match[1]),
  );
}

export function LayoutEditorToolbar({
  canRedo,
  canUndo,
  dirty,
  onCancel,
  onRedo,
  onReset,
  onSave,
  onUndo,
  saving,
}: {
  canRedo: boolean;
  canUndo: boolean;
  dirty: boolean;
  onCancel: () => void;
  onRedo: () => void;
  onReset: () => void;
  onSave: () => void;
  onUndo: () => void;
  saving: boolean;
}) {
  return (
    <section className="layout-toolbar" aria-label="Dashboard layout editor">
      <div>
        <strong>Edit dashboard</strong>
        <span> Changes stay in memory until you save.</span>
      </div>
      <div className="layout-actions">
        <button
          className="secondary"
          disabled={!canUndo}
          onClick={onUndo}
          type="button"
        >
          Undo
        </button>
        <button
          className="secondary"
          disabled={!canRedo}
          onClick={onRedo}
          type="button"
        >
          Redo
        </button>
        <button className="secondary" onClick={onReset} type="button">
          Reset draft
        </button>
        <button className="secondary" onClick={onCancel} type="button">
          Cancel
        </button>
        <button disabled={!dirty || saving} onClick={onSave} type="button">
          {saving ? "Saving…" : "Save layout"}
        </button>
      </div>
    </section>
  );
}

export function WidgetPalette({
  layout,
  onAdd,
}: {
  layout: DashboardLayoutV1;
  onAdd: (type: DashboardWidgetType) => void;
}) {
  return (
    <aside className="widget-palette" aria-label="Widget catalog">
      <h2>Widget catalog</h2>
      <p>Additions use the first free position in row-major grid order.</p>
      <div className="widget-palette-list">
        {(Object.keys(DASHBOARD_WIDGET_REGISTRY) as DashboardWidgetType[]).map(
          (type) => {
            const entry = WIDGET_CATALOG[type];
            const limits = DASHBOARD_WIDGET_REGISTRY[type];
            return (
              <article className="widget-preview" key={type}>
                <h3>{entry.title}</h3>
                <p>{entry.description}</p>
                <small>
                  {entry.metrics} · {limits.minWidth}–{limits.maxWidth} columns ·{" "}
                  {limits.minHeight}–{limits.maxHeight} rows
                </small>
                <button type="button" onClick={() => onAdd(type)}>
                  Add {entry.title}
                </button>
              </article>
            );
          },
        )}
      </div>
      <p>{layout.widgets.length} of 32 widget instances used.</p>
    </aside>
  );
}

function deltaForKey(event: KeyboardEvent<HTMLButtonElement>) {
  const resize = event.shiftKey;
  if (event.key === "ArrowLeft") return resize ? { width: -1 } : { x: -1 };
  if (event.key === "ArrowRight") return resize ? { width: 1 } : { x: 1 };
  if (event.key === "ArrowUp") return resize ? { height: -1 } : { y: -1 };
  if (event.key === "ArrowDown") return resize ? { height: 1 } : { y: 1 };
  return null;
}

export function WidgetEditorControls({
  onChange,
  onRemove,
  widget,
}: {
  onChange: (
    id: string,
    delta: { height?: number; width?: number; x?: number; y?: number },
  ) => void;
  onRemove: (id: string) => void;
  widget: DashboardWidgetV1;
}) {
  const title = WIDGET_CATALOG[widget.type].title;
  const pointer = useRef<
    | {
        geometry: PointerGridGeometry;
        id: number;
        startX: number;
        startY: number;
      }
    | undefined
  >(undefined);

  function pointerDown(event: PointerEvent<HTMLButtonElement>) {
    const grid = event.currentTarget.closest(".dashboard-layout-grid");
    if (!(grid instanceof HTMLElement) || matchMedia("(max-width: 52rem)").matches)
      return;
    const style = getComputedStyle(grid);
    const columns = pixelTracks(style.gridTemplateColumns);
    const rows = pixelTracks(style.gridTemplateRows);
    if (columns.length !== DASHBOARD_GRID_COLUMNS || rows.length <= widget.position.y)
      return;
    pointer.current = {
      geometry: {
        columnGap: Number.parseFloat(style.columnGap) || 0,
        columns,
        rowGap: Number.parseFloat(style.rowGap) || 0,
        rows,
        startX: widget.position.x,
        startY: widget.position.y,
      },
      id: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
    };
    event.currentTarget.setPointerCapture(event.pointerId);
  }

  function pointerUp(event: PointerEvent<HTMLButtonElement>) {
    const start = pointer.current;
    if (!start || start.id !== event.pointerId) return;
    const { x, y } = pointerGridDelta(
      start.geometry,
      event.clientX - start.startX,
      event.clientY - start.startY,
    );
    event.currentTarget.releasePointerCapture(event.pointerId);
    pointer.current = undefined;
    if (x || y) onChange(widget.id, { x, y });
  }

  function pointerCancel(event: PointerEvent<HTMLButtonElement>) {
    if (pointer.current?.id === event.pointerId) pointer.current = undefined;
  }

  function keyboard(event: KeyboardEvent<HTMLButtonElement>) {
    const delta = deltaForKey(event);
    if (!delta) return;
    event.preventDefault();
    onChange(widget.id, delta);
  }

  return (
    <fieldset className="widget-editor-controls">
      <legend className="sr-only">Edit {title}</legend>
      <button
        className="widget-drag-handle"
        aria-label={`Move or resize ${title}. Arrow keys move; Shift plus arrow keys resize.`}
        onKeyDown={keyboard}
        onPointerCancel={pointerCancel}
        onPointerDown={pointerDown}
        onPointerUp={pointerUp}
        type="button"
      >
        Move
      </button>
      <div className="widget-nudge-controls">
        <button
          aria-label={`Move ${title} left`}
          onClick={() => onChange(widget.id, { x: -1 })}
          type="button"
        >
          ←
        </button>
        <button
          aria-label={`Move ${title} up`}
          onClick={() => onChange(widget.id, { y: -1 })}
          type="button"
        >
          ↑
        </button>
        <button
          aria-label={`Move ${title} down`}
          onClick={() => onChange(widget.id, { y: 1 })}
          type="button"
        >
          ↓
        </button>
        <button
          aria-label={`Move ${title} right`}
          onClick={() => onChange(widget.id, { x: 1 })}
          type="button"
        >
          →
        </button>
        <button
          aria-label={`Narrow ${title}`}
          onClick={() => onChange(widget.id, { width: -1 })}
          type="button"
        >
          −W
        </button>
        <button
          aria-label={`Widen ${title}`}
          onClick={() => onChange(widget.id, { width: 1 })}
          type="button"
        >
          +W
        </button>
        <button
          aria-label={`Shorten ${title}`}
          onClick={() => onChange(widget.id, { height: -1 })}
          type="button"
        >
          −H
        </button>
        <button
          aria-label={`Make ${title} taller`}
          onClick={() => onChange(widget.id, { height: 1 })}
          type="button"
        >
          +H
        </button>
      </div>
      <button
        aria-label={`Remove ${title}`}
        className="widget-remove"
        onClick={() => onRemove(widget.id)}
        type="button"
      >
        Remove
      </button>
    </fieldset>
  );
}
