"use client";

import { useState } from "react";
import { cn } from "@/lib/cn";
import { formatCompact, formatMeasure } from "@/lib/measureFormat";
import type { SemanticMeasure } from "@/lib/types";

export interface BarDatum {
  label: string;
  value: number | null;
  raw: unknown;
}

// Mark spec: bars are capped rather than filling their band, so the leftover is air.
const MAX_BAR = 22;
const MIN_BAR = 8;
const ROW_GAP = 10;
const LABEL_WIDTH = 108;
const VALUE_WIDTH = 78;

/**
 * Horizontal bars for magnitude by category.
 *
 * Horizontal because category names are words, not dates - they read straight and
 * never need rotating. One series, so one hue (slot 1) and no legend: the tile title
 * already says what is plotted, and colouring bars by their own size would burn the
 * only free channel restating the length.
 */
export function BarChart({
  data,
  measure,
  height,
  onSelect,
  selected,
}: {
  data: BarDatum[];
  measure: SemanticMeasure | undefined;
  height: number;
  onSelect?: (datum: BarDatum) => void;
  selected?: string | null;
}) {
  const [hovered, setHovered] = useState<number | null>(null);

  if (data.length === 0) {
    return (
      <p className="flex h-full items-center justify-center text-[12px] text-[var(--text-subtle)]">
        No rows match the current filters.
      </p>
    );
  }

  const values = data.map((d) => d.value ?? 0);
  const max = Math.max(...values, 0);
  // A bar chart's baseline is always zero; starting elsewhere exaggerates differences.
  const scale = max > 0 ? 1 / max : 0;

  const available = height - 8;
  const band = Math.min(MAX_BAR + ROW_GAP, Math.max(MIN_BAR + 6, available / data.length));
  const barHeight = Math.min(MAX_BAR, Math.max(MIN_BAR, band - ROW_GAP));

  return (
    <div className="h-full overflow-y-auto pr-1" role="figure">
      <div style={{ minHeight: data.length * band }}>
        {data.map((datum, index) => {
          const fraction = (datum.value ?? 0) * scale;
          const isSelected = selected != null && datum.label === selected;
          const isDimmed = selected != null && !isSelected;
          const isHovered = hovered === index;

          return (
            <div
              key={`${datum.label}-${index}`}
              className={cn(
                "group flex items-center gap-2 rounded-[var(--radius-sm)] transition-opacity",
                onSelect && "cursor-pointer",
                isDimmed && "opacity-35",
              )}
              style={{ height: band }}
              onMouseEnter={() => setHovered(index)}
              onMouseLeave={() => setHovered(null)}
              onFocus={() => setHovered(index)}
              onBlur={() => setHovered(null)}
              onClick={() => onSelect?.(datum)}
              tabIndex={onSelect ? 0 : undefined}
              onKeyDown={(event) => {
                if (onSelect && (event.key === "Enter" || event.key === " ")) {
                  event.preventDefault();
                  onSelect(datum);
                }
              }}
              title={
                measure
                  ? `${datum.label}: ${formatMeasure(datum.value, measure)}`
                  : `${datum.label}: ${datum.value}`
              }
            >
              <span
                className="shrink-0 truncate text-right text-[11.5px] text-[var(--text-muted)]"
                style={{ width: LABEL_WIDTH }}
              >
                {datum.label}
              </span>

              <span className="relative min-w-0 flex-1">
                {/* Track sits one step off the surface so the bar reads as the data. */}
                <span
                  className="absolute inset-y-0 my-auto block w-full rounded-[3px] bg-[var(--surface-sunken)]"
                  style={{ height: barHeight }}
                  aria-hidden
                />
                <span
                  className="absolute inset-y-0 my-auto block transition-[width,filter] duration-150"
                  style={{
                    height: barHeight,
                    width: `${Math.max(fraction * 100, datum.value ? 1.5 : 0)}%`,
                    background: "var(--series-1)",
                    // Square at the baseline, 4px rounded at the data end.
                    borderRadius: "0 4px 4px 0",
                    filter: isHovered || isSelected ? "brightness(1.14)" : undefined,
                  }}
                />
              </span>

              <span
                className="shrink-0 text-right text-[11.5px] font-medium tabular-nums text-[var(--text)]"
                style={{ width: VALUE_WIDTH }}
              >
                {measure ? formatCompact(datum.value, measure) : String(datum.value ?? "—")}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
