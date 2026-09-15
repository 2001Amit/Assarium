"use client";

import { useMemo, useState } from "react";
import { formatAxis, formatMeasure } from "@/lib/measureFormat";
import { seriesColor } from "@/lib/palette";
import type { SemanticMeasure } from "@/lib/types";

export interface ScatterPoint {
  label: string;
  x: number | null;
  y: number | null;
  raw: unknown;
}

const PADDING = { top: 16, right: 20, bottom: 34, left: 58 };

/**
 * The height this chart must leave for what sits under the plot.
 *
 * The tile measures its body with a ResizeObserver and passes the result in as `height`.
 * If the rendered content is ever *taller* than that - an SVG at the full height plus a
 * legend beneath it - the tile grows, the observer reports the larger box, the SVG grows
 * to match, and the tile stretches down the page a few pixels at a time. The tile sets a
 * `minHeight` rather than a fixed height, so nothing stops it.
 *
 * The invariant that prevents it: plot height + footer height == the height we were given.
 */
const FOOTER_HEIGHT = 20;

/**
 * Two measures against each other, one dot per category.
 *
 * The chart for "does A move with B" - rent against floor area, spend against
 * conversion. Unlike the other charts here, neither axis starts at zero by default: a
 * scatter is read as a cloud shape, and forcing a zero origin squashes the cloud into a
 * corner where no relationship is visible.
 *
 * No trend line is drawn. A fitted line invites the reader to treat correlation as
 * causation, and on this much data it is usually fitting noise - if the relationship is
 * real, the cloud already shows it.
 */
export function ScatterChart({
  points,
  xMeasure,
  yMeasure,
  width,
  height,
  onSelect,
  selected,
}: {
  points: ScatterPoint[];
  xMeasure: SemanticMeasure | undefined;
  yMeasure: SemanticMeasure | undefined;
  width: number;
  height: number;
  onSelect?: (point: ScatterPoint) => void;
  selected?: string | null;
}) {
  const [hovered, setHovered] = useState<number | null>(null);

  const usable = useMemo(
    () => points.filter((p) => p.x !== null && p.y !== null),
    [points],
  );

  const bounds = useMemo(() => {
    if (usable.length === 0) return null;
    const xs = usable.map((p) => p.x as number);
    const ys = usable.map((p) => p.y as number);
    return {
      x: pad(Math.min(...xs), Math.max(...xs)),
      y: pad(Math.min(...ys), Math.max(...ys)),
    };
  }, [usable]);

  if (usable.length === 0 || bounds === null) {
    return (
      <p className="flex h-full items-center justify-center px-4 text-center text-[12px] text-[var(--text-subtle)]">
        {points.length === 0
          ? "No rows match the current filters."
          : "Every row is missing one of the two measures, so there is nothing to plot."}
      </p>
    );
  }

  const svgHeight = Math.max(60, height - FOOTER_HEIGHT);
  const plotWidth = Math.max(10, width - PADDING.left - PADDING.right);
  const plotHeight = Math.max(10, svgHeight - PADDING.top - PADDING.bottom);

  const xAt = (value: number) =>
    PADDING.left + ((value - bounds.x.lo) / bounds.x.span) * plotWidth;
  const yAt = (value: number) =>
    PADDING.top + plotHeight - ((value - bounds.y.lo) / bounds.y.span) * plotHeight;

  const focus = hovered !== null ? usable[hovered] : null;

  return (
    <div className="flex h-full flex-col overflow-hidden">
      <svg width={width} height={svgHeight} role="img" aria-label="Two measures compared">
        {[0, 0.25, 0.5, 0.75, 1].map((fraction) => {
          const y = PADDING.top + plotHeight * fraction;
          return (
            <g key={`h-${fraction}`}>
              <line
                x1={PADDING.left}
                x2={PADDING.left + plotWidth}
                y1={y}
                y2={y}
                stroke="var(--border-subtle)"
              />
              <text
                x={PADDING.left - 8}
                y={y + 3}
                textAnchor="end"
                fill="var(--text-subtle)"
                className="text-[10px] tabular-nums"
              >
                {formatAxis(bounds.y.hi - bounds.y.span * fraction, yMeasure)}
              </text>
            </g>
          );
        })}

        {[0, 0.5, 1].map((fraction) => (
          <text
            key={`v-${fraction}`}
            x={PADDING.left + plotWidth * fraction}
            y={svgHeight - 16}
            textAnchor={fraction === 0 ? "start" : fraction === 1 ? "end" : "middle"}
            fill="var(--text-subtle)"
            className="text-[10px] tabular-nums"
          >
            {formatAxis(bounds.x.lo + bounds.x.span * fraction, xMeasure)}
          </text>
        ))}

        {usable.map((point, index) => {
          const dim = selected != null && selected !== point.label;
          return (
            <circle
              key={point.label}
              cx={xAt(point.x as number)}
              cy={yAt(point.y as number)}
              r={hovered === index ? 6 : 4.5}
              fill={seriesColor(0)}
              // Overlapping dots are the normal case, so they are translucent: a dark
              // patch then genuinely means "many points here" rather than "one on top".
              opacity={dim ? 0.2 : 0.62}
              className="cursor-pointer transition-all"
              onMouseEnter={() => setHovered(index)}
              onMouseLeave={() => setHovered(null)}
              onClick={() => onSelect?.(point)}
            />
          );
        })}

        <text
          x={PADDING.left + plotWidth / 2}
          y={svgHeight - 2}
          textAnchor="middle"
          fill="var(--text-subtle)"
          className="text-[10px]"
        >
          {xMeasure?.label ?? "x"}
        </text>
      </svg>

      <div
        className="shrink-0 overflow-hidden whitespace-nowrap px-1 text-[11px] text-[var(--text-subtle)]"
        style={{ height: FOOTER_HEIGHT }}
      >
        {focus ? (
          <span>
            <span className="text-[var(--text)]">{focus.label}</span>
            {" · "}
            {xMeasure ? formatMeasure(focus.x, xMeasure) : focus.x}
            {" · "}
            {yMeasure ? formatMeasure(focus.y, yMeasure) : focus.y}
          </span>
        ) : (
          <span>
            {yMeasure?.label ?? "y"} against {xMeasure?.label?.toLowerCase() ?? "x"} ·{" "}
            {usable.length} point{usable.length === 1 ? "" : "s"}
          </span>
        )}
      </div>
    </div>
  );
}

/**
 * A range with breathing room at both ends.
 *
 * Not anchored at zero, unlike the bar and line charts: a scatter is about the shape of
 * the cloud, and a forced zero origin pushes it into a corner where any relationship
 * becomes invisible.
 */
function pad(lo: number, hi: number): { lo: number; hi: number; span: number } {
  if (lo === hi) {
    const nudge = Math.abs(lo) * 0.1 || 1;
    return { lo: lo - nudge, hi: hi + nudge, span: nudge * 2 };
  }
  const margin = (hi - lo) * 0.08;
  return { lo: lo - margin, hi: hi + margin, span: hi - lo + margin * 2 };
}
