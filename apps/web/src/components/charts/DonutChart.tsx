"use client";

import { useState } from "react";
import { formatMeasure } from "@/lib/measureFormat";
import { seriesColor } from "@/lib/palette";
import type { SemanticMeasure } from "@/lib/types";

export interface Slice {
  label: string;
  value: number | null;
  raw: unknown;
}

const THICKNESS = 26;
const GAP_DEGREES = 1.2;

/**
 * Share of a total.
 *
 * A donut rather than a pie: the hole carries the total, which is the number people
 * actually want, and comparing arc lengths at a constant radius is slightly easier
 * than comparing wedge areas. Both are still weak at comparing similar values, so this
 * is only ever drawn for a handful of categories - the server folds the tail into
 * "Other" before it gets here, and refuses the chart outright past that.
 *
 * Negative values are excluded rather than drawn. A share of a total is not defined when
 * a part is negative, and rendering it anyway produces a chart that silently lies.
 */
export function DonutChart({
  data,
  measure,
  size,
  width,
  onSelect,
  selected,
}: {
  data: Slice[];
  measure: SemanticMeasure | undefined;
  size: number;
  /** Tile width, so the legend can move below the ring when there is no room beside it. */
  width?: number;
  onSelect?: (slice: Slice) => void;
  selected?: string | null;
}) {
  const [hovered, setHovered] = useState<number | null>(null);

  const usable = data.filter((d) => (d.value ?? 0) > 0);
  const dropped = data.length - usable.length;
  const total = usable.reduce((sum, d) => sum + (d.value ?? 0), 0);

  if (usable.length === 0 || total <= 0) {
    return (
      <p className="flex h-full items-center justify-center px-4 text-center text-[12px] text-[var(--text-subtle)]">
        {data.length === 0
          ? "No rows match the current filters."
          : "Every value here is zero or negative, so there is no share to show."}
      </p>
    );
  }

  // Side by side needs room for both. Below a threshold the legend gets squeezed into
  // ellipses, at which point the labels have stopped being labels - so it moves under
  // the ring instead, where it has the full width.
  const stacked = (width ?? 999) < 340;
  const box = Math.max(110, Math.min(size, stacked ? 150 : 260));
  const radius = box / 2;
  const inner = radius - THICKNESS;

  let cursor = -90; // start at twelve o'clock, where a reader starts
  const arcs = usable.map((slice, index) => {
    const fraction = (slice.value ?? 0) / total;
    const sweep = fraction * 360;
    const start = cursor;
    cursor += sweep;
    return { slice, index, start, sweep, fraction };
  });

  const active = hovered !== null ? arcs[hovered] : null;
  const focus = active ?? null;

  return (
    <div
      className={
        stacked
          ? "flex h-full flex-col items-center gap-2 overflow-hidden"
          : "flex h-full items-center gap-5"
      }
    >
      <svg
        width={box}
        height={box}
        viewBox={`0 0 ${box} ${box}`}
        role="img"
        aria-label={`Share by category, ${usable.length} slices`}
        className="shrink-0"
      >
        <g transform={`translate(${radius} ${radius})`}>
          {arcs.map(({ slice, index, start, sweep }) => {
            const dim = selected != null && selected !== slice.label;
            return (
              <path
                key={slice.label}
                d={arcPath(start + GAP_DEGREES / 2, sweep - GAP_DEGREES, radius - 1, inner)}
                fill={seriesColor(index)}
                opacity={dim ? 0.28 : hovered === null || hovered === index ? 1 : 0.45}
                className="cursor-pointer transition-opacity"
                onMouseEnter={() => setHovered(index)}
                onMouseLeave={() => setHovered(null)}
                onClick={() => onSelect?.(slice)}
              />
            );
          })}

          {/* The hole is not decoration - it is where the total goes. */}
          <text
            textAnchor="middle"
            y={focus ? -4 : 2}
            className="fill-[var(--text)] text-[15px] font-medium tabular-nums"
          >
            {measure
              ? formatMeasure(focus ? focus.slice.value : total, measure)
              : String(focus ? focus.slice.value : total)}
          </text>
          <text
            textAnchor="middle"
            y={focus ? 13 : 18}
            className="fill-[var(--text-subtle)] text-[11px]"
          >
            {focus ? `${(focus.fraction * 100).toFixed(1)}%` : "Total"}
          </text>
        </g>
      </svg>

      <ul
        className={
          stacked
            ? "w-full min-h-0 flex-1 space-y-1 overflow-y-auto"
            : "min-w-0 flex-1 space-y-1.5 overflow-hidden"
        }
      >
        {arcs.map(({ slice, index, fraction }) => (
          <li
            key={slice.label}
            className="flex cursor-pointer items-center gap-2 text-[12px]"
            onMouseEnter={() => setHovered(index)}
            onMouseLeave={() => setHovered(null)}
            onClick={() => onSelect?.(slice)}
          >
            <span
              className="h-2.5 w-2.5 shrink-0 rounded-[2px]"
              style={{ background: seriesColor(index) }}
            />
            <span className="min-w-0 flex-1 truncate text-[var(--text)]">{slice.label}</span>
            <span className="shrink-0 tabular-nums text-[var(--text-subtle)]">
              {(fraction * 100).toFixed(1)}%
            </span>
          </li>
        ))}
        {dropped > 0 && (
          <li className="pt-1 text-[11px] text-[var(--text-subtle)]">
            {dropped} categor{dropped === 1 ? "y is" : "ies are"} zero or negative and
            cannot be shown as a share.
          </li>
        )}
      </ul>
    </div>
  );
}

/** One ring segment, as an SVG path. Angles in degrees, clockwise from twelve. */
function arcPath(startDeg: number, sweepDeg: number, outer: number, inner: number): string {
  const sweep = Math.max(sweepDeg, 0.4);
  const a0 = (startDeg * Math.PI) / 180;
  const a1 = ((startDeg + sweep) * Math.PI) / 180;
  const large = sweep > 180 ? 1 : 0;

  const x0 = Math.cos(a0) * outer;
  const y0 = Math.sin(a0) * outer;
  const x1 = Math.cos(a1) * outer;
  const y1 = Math.sin(a1) * outer;
  const x2 = Math.cos(a1) * inner;
  const y2 = Math.sin(a1) * inner;
  const x3 = Math.cos(a0) * inner;
  const y3 = Math.sin(a0) * inner;

  return [
    `M ${x0} ${y0}`,
    `A ${outer} ${outer} 0 ${large} 1 ${x1} ${y1}`,
    `L ${x2} ${y2}`,
    `A ${inner} ${inner} 0 ${large} 0 ${x3} ${y3}`,
    "Z",
  ].join(" ");
}
