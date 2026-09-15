"use client";

import { ArrowRight, Table2 } from "lucide-react";
import { cn } from "@/lib/cn";
import { formatCount } from "@/lib/format";
import type { Layer, Warehouse } from "@/lib/types";
import { LAYER_COLORS } from "@/components/datasets/LayerBadge";

const LAYER_ORDER: Layer[] = ["bronze", "silver", "gold"];

const LAYER_INTENT: Record<Layer, string> = {
  bronze: "Landed exactly as the source gave it. Nothing here is corrected, so a run can always be repeated.",
  silver: "Typed, trimmed, deduplicated and keyed. This is the layer everything else is derived from.",
  gold: "Aggregated to a reporting grain, ready for dashboards and questions.",
};

export function MedallionView({
  warehouse,
  selected,
  onSelect,
}: {
  warehouse: Warehouse;
  selected: { layer: Layer; table: string } | null;
  onSelect: (layer: Layer, table: string, rows: number) => void;
}) {
  return (
    <div className="grid gap-3 p-4 lg:grid-cols-3">
      {LAYER_ORDER.map((layer, index) => {
        const tables = warehouse.layers[layer] ?? [];
        const color = LAYER_COLORS[layer].color;
        const totalRows = tables.reduce((sum, table) => sum + table.rows, 0);

        return (
          <section key={layer} className="relative min-w-0">
            {index < 2 && (
              <ArrowRight
                className="absolute -right-2.5 top-3 z-10 hidden size-3 text-[var(--text-subtle)] lg:block"
                aria-hidden
              />
            )}

            <div className="surface-panel flex h-full flex-col overflow-hidden">
              <header
                className="shrink-0 border-b border-[var(--line)] px-3 py-2"
                style={{ background: `color-mix(in srgb, ${color} 5%, transparent)` }}
              >
                <div className="flex items-center gap-2">
                  <span
                    className="size-2 rounded-full"
                    style={{ background: color }}
                    aria-hidden
                  />
                  <h2
                    className="text-[12.5px] font-semibold capitalize"
                    style={{ color }}
                  >
                    {layer}
                  </h2>
                  <span className="ml-auto text-[11px] tabular-nums text-[var(--text-subtle)]">
                    {tables.length} {tables.length === 1 ? "table" : "tables"} ·{" "}
                    {formatCount(totalRows)} rows
                  </span>
                </div>
                <p className="mt-1 text-[11.5px] leading-relaxed text-[var(--text-muted)]">
                  {LAYER_INTENT[layer]}
                </p>
              </header>

              <ul className="min-h-[92px] flex-1 p-1">
                {tables.length === 0 ? (
                  <li className="px-2.5 py-4 text-center text-[11.5px] text-[var(--text-subtle)]">
                    Empty
                  </li>
                ) : (
                  tables.map((table) => {
                    const active =
                      selected?.layer === layer && selected.table === table.name;
                    return (
                      <li key={table.name}>
                        <button
                          onClick={() => onSelect(layer, table.name, table.rows)}
                          className={cn(
                            "flex w-full items-center gap-2 rounded-[var(--radius-sm)] px-2 py-1.5 text-left",
                            active
                              ? "bg-[var(--accent-soft)]"
                              : "hover:bg-[var(--surface-hover)]",
                          )}
                        >
                          <Table2
                            className="size-3.5 shrink-0 text-[var(--text-subtle)]"
                            aria-hidden
                          />
                          <span className="min-w-0 flex-1 truncate font-mono text-[11.5px] text-[var(--text)]">
                            {table.name}
                          </span>
                          <span className="shrink-0 text-[11px] tabular-nums text-[var(--text-subtle)]">
                            {formatCount(table.rows)}
                          </span>
                          <span className="shrink-0 text-[10.5px] tabular-nums text-[var(--text-subtle)]">
                            {table.columns.length}c
                          </span>
                        </button>
                      </li>
                    );
                  })
                )}
              </ul>
            </div>
          </section>
        );
      })}
    </div>
  );
}
