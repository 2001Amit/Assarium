"use client";

import { useState } from "react";
import { formatMeasure } from "@/lib/measureFormat";
import { seriesColor } from "@/lib/palette";
import type { SemanticMeasure } from "@/lib/types";

export interface StackRow {
  label: string;
  values: Record<string, number | null>;
  raw: unknown;
}

const ROW_HEIGHT = 26;
const BAR_HEIGHT = 16;
const LABEL_WIDTH = 116;
const TOTAL_WIDTH = 80;

/**
 * Several measures across one dimension, as parts of a whole.
 *
 * Horizontal, like the plain bar chart, because category names read straight. Stacked
 * rather than grouped because the question this answers is "what makes up the total" -
 * grouped bars answer "how do these compare", which the plain bar chart already does
 * one measure at a time.
 *
 * Only the first band starts at a shared baseline, so only the first band is comparable
 * by eye across rows. That is an inherent property of stacking, not something styling
 * can fix, so the total is printed for every row and the bands carry values on hover.
 * Negative values are excluded: a part cannot be a negative share of a whole.
 */
export function StackedBarChart({
  rows,
  measures,
  width,
  height,
  onSelect,
  selected,
}: {
  rows: StackRow[];
  measures: SemanticMeasure[];
  width: number;
  height: number;
  onSelect?: (row: StackRow) => void;
  selected?: string | null;
}) {
  const [hovered, setHovered] = useState<{ row: number; band: number } | null>(null);

  if (rows.length === 0 || measures.length === 0) {
    return (
      <p className="flex h-full items-center justify-center text-[12px] text-[var(--text-subtle)]">
        No rows match the current filters.
      </p>
    );
  }

  const valueOf = (row: StackRow, measure: SemanticMeasure) => {
    const value = row.values[measure.name] ?? row.values[measure.id];
    return typeof value === "number" && value > 0 ? value : 0;
  };

  const totals = rows.map((row) =>
    measures.reduce((sum, measure) => sum + valueOf(row, measure), 0),
  );
  const max = Math.max(...totals, 0);
  const trackWidth = Math.max(40, width - LABEL_WIDTH - TOTAL_WIDTH - 16);
  const visible = Math.max(1, Math.floor((height - 24) / ROW_HEIGHT));

  const focus = hovered ? { ...hovered } : null;

  return (
    <div className="flex h-full flex-col">
      <div className="flex flex-wrap gap-x-3 gap-y-1 pb-2 text-[11px]">
        {measures.map((measure, index) => (
          <span key={measure.id} className="flex items-center gap-1.5 text-[var(--text-subtle)]">
            <span
              className="h-2.5 w-2.5 rounded-[2px]"
              style={{ background: seriesColor(index) }}
            />
            {measure.label}
          </span>
        ))}
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        {rows.slice(0, Math.max(visible, rows.length)).map((row, rowIndex) => {
          const total = totals[rowIndex];
          const dim = selected != null && selected !== row.label;
          let offset = 0;

          return (
            <div
              key={row.label}
              className="flex cursor-pointer items-center gap-3"
              style={{ height: ROW_HEIGHT }}
              onClick={() => onSelect?.(row)}
              onMouseLeave={() => setHovered(null)}
            >
              <span
                className="shrink-0 truncate text-[12px] text-[var(--text)]"
                style={{ width: LABEL_WIDTH }}
                title={row.label}
              >
                {row.label}
              </span>

              <div
                className="relative shrink-0 overflow-hidden rounded-[3px] bg-[var(--surface-sunken)]"
                style={{ width: trackWidth, height: BAR_HEIGHT }}
              >
                {measures.map((measure, bandIndex) => {
                  const value = valueOf(row, measure);
                  const bandWidth = max > 0 ? (value / max) * trackWidth : 0;
                  const left = offset;
                  offset += bandWidth;
                  if (bandWidth <= 0) return null;
                  const isFocus = focus?.row === rowIndex && focus.band === bandIndex;
                  return (
                    <div
                      key={measure.id}
                      className="absolute top-0 h-full transition-opacity"
                      style={{
                        left,
                        width: bandWidth,
                        background: seriesColor(bandIndex),
                        opacity: dim ? 0.25 : focus && !isFocus ? 0.55 : 1,
                      }}
                      onMouseEnter={() => setHovered({ row: rowIndex, band: bandIndex })}
                      title={`${measure.label}: ${formatMeasure(value, measure)}`}
                    />
                  );
                })}
              </div>

              <span
                className="shrink-0 text-right text-[12px] tabular-nums text-[var(--text-subtle)]"
                style={{ width: TOTAL_WIDTH }}
              >
                {formatMeasure(total, measures[0])}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
