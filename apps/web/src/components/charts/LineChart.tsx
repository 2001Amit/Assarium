"use client";

import { useMemo, useRef, useState } from "react";
import { formatAxis, formatMeasure } from "@/lib/measureFormat";
import type { SemanticMeasure } from "@/lib/types";

export interface LinePoint {
  label: string;
  value: number | null;
}

const PADDING = { top: 14, right: 18, bottom: 24, left: 52 };

/**
 * A single series over time.
 *
 * One measure, one axis. Two measures of different scale would need two y-scales,
 * which invents a correlation that is not in the data - they get two charts instead.
 * The crosshair snaps to the nearest period so the reader aims at a date, never at a
 * 2px line.
 */
export function LineChart({
  points,
  measure,
  width,
  height,
  filled = false,
}: {
  points: LinePoint[];
  measure: SemanticMeasure | undefined;
  width: number;
  height: number;
  /**
   * Draw it as an area rather than a line.
   *
   * Not a style choice. A filled shape reads as an accumulated quantity - how much there
   * was - while a line reads as a rate or a level. A filled stock price is misleading in
   * a way a filled revenue total is not, so the tile type decides this, never the theme.
   */
  filled?: boolean;
}) {
  const svgRef = useRef<SVGSVGElement>(null);
  const [active, setActive] = useState<number | null>(null);

  const plotWidth = Math.max(10, width - PADDING.left - PADDING.right);
  const plotHeight = Math.max(10, height - PADDING.top - PADDING.bottom);

  const { coords, ticks, min, span } = useMemo(() => {
    const values = points.map((p) => p.value).filter((v): v is number => v !== null);
    if (values.length === 0) {
      return { coords: [], ticks: [] as number[], min: 0, span: 1 };
    }
    const rawMax = Math.max(...values);
    // A trend of positive values reads honestly from a zero baseline.
    const lo = Math.min(0, ...values);
    const hi = niceCeiling(rawMax);
    const range = hi - lo || 1;

    const step = points.length > 1 ? plotWidth / (points.length - 1) : 0;
    const mapped = points.map((point, index) => ({
      x: PADDING.left + index * step,
      y:
        point.value === null
          ? null
          : PADDING.top + plotHeight * (1 - (point.value - lo) / range),
      point,
    }));

    return { coords: mapped, ticks: niceTicks(lo, hi, 4), min: lo, span: range };
  }, [points, plotWidth, plotHeight]);

  if (points.length === 0) {
    return (
      <p className="flex h-full items-center justify-center text-[12px] text-[var(--text-subtle)]">
        No rows match the current filters.
      </p>
    );
  }

  const drawn = coords.filter(
    (c): c is { x: number; y: number; point: LinePoint } => c.y !== null,
  );
  const path = drawn.map((c, i) => `${i === 0 ? "M" : "L"}${c.x},${c.y}`).join(" ");
  const areaPath =
    drawn.length > 0
      ? `${path} L${drawn[drawn.length - 1].x},${PADDING.top + plotHeight} L${drawn[0].x},${
          PADDING.top + plotHeight
        } Z`
      : "";

  // Label the last point only: a number on every point goes unread.
  const last = drawn[drawn.length - 1];
  const activeCoord = active !== null ? coords[active] : null;

  const onMove = (event: React.PointerEvent<SVGSVGElement>) => {
    const rect = svgRef.current?.getBoundingClientRect();
    if (!rect || points.length < 2) return;
    const x = event.clientX - rect.left;
    const step = plotWidth / (points.length - 1);
    const index = Math.round((x - PADDING.left) / step);
    setActive(index >= 0 && index < points.length ? index : null);
  };

  return (
    <div className="relative h-full w-full">
      <svg
        ref={svgRef}
        width={width}
        height={height}
        onPointerMove={onMove}
        onPointerLeave={() => setActive(null)}
        role="figure"
        aria-label={measure ? `${measure.label} over time` : "Trend"}
      >
        {ticks.map((tick) => {
          const y = PADDING.top + plotHeight * (1 - (tick - min) / span);
          return (
            <g key={tick}>
              {/* Hairline, solid, one step off the surface. Never dashed. */}
              <line
                x1={PADDING.left}
                x2={width - PADDING.right}
                y1={y}
                y2={y}
                stroke="var(--line)"
                strokeWidth={1}
              />
              <text
                x={PADDING.left - 8}
                y={y + 3}
                textAnchor="end"
                className="fill-[var(--text-subtle)] tabular-nums"
                style={{ fontSize: 10 }}
              >
                {formatAxis(tick, measure)}
              </text>
            </g>
          );
        })}

        <path d={areaPath} fill="var(--series-1)" opacity={filled ? 0.28 : 0.1} />
        <path
          d={path}
          fill="none"
          stroke="var(--series-1)"
          strokeWidth={2}
          strokeLinejoin="round"
          strokeLinecap="round"
        />

        {activeCoord?.y != null && (
          <>
            <line
              x1={activeCoord.x}
              x2={activeCoord.x}
              y1={PADDING.top}
              y2={PADDING.top + plotHeight}
              stroke="var(--line-strong)"
              strokeWidth={1}
            />
            <circle
              cx={activeCoord.x}
              cy={activeCoord.y}
              r={4.5}
              fill="var(--series-1)"
              stroke="var(--surface)"
              strokeWidth={2}
            />
          </>
        )}

        {last && (
          <circle
            cx={last.x}
            cy={last.y}
            r={4}
            fill="var(--series-1)"
            stroke="var(--surface)"
            strokeWidth={2}
          />
        )}

        {coords.map((coord, index) => {
          // Show roughly six x labels regardless of series length.
          const stride = Math.max(1, Math.ceil(points.length / 6));
          if (index % stride !== 0 && index !== points.length - 1) return null;
          return (
            <text
              key={index}
              x={coord.x}
              y={height - 7}
              textAnchor={index === points.length - 1 ? "end" : "middle"}
              className="fill-[var(--text-subtle)]"
              style={{ fontSize: 10 }}
            >
              {coord.point.label}
            </text>
          );
        })}
      </svg>

      {activeCoord && (
        <div
          className="pointer-events-none absolute z-10 -translate-x-1/2 rounded-[var(--radius-md)] border border-[var(--line)] bg-[var(--surface)] px-2 py-1.5 shadow-[0_4px_16px_-4px_rgba(0,0,0,0.25)]"
          style={{
            left: Math.min(Math.max(activeCoord.x, 60), width - 60),
            top: PADDING.top - 4,
          }}
        >
          {/* Value leads, label follows: the reader has the series and wants the number. */}
          <p className="text-[12.5px] font-semibold tabular-nums text-[var(--text)]">
            {measure
              ? formatMeasure(activeCoord.point.value, measure)
              : String(activeCoord.point.value ?? "—")}
          </p>
          <p className="flex items-center gap-1.5 text-[11px] text-[var(--text-muted)]">
            <span
              className="inline-block h-0.5 w-3 rounded-full"
              style={{ background: "var(--series-1)" }}
              aria-hidden
            />
            {activeCoord.point.label}
          </p>
        </div>
      )}
    </div>
  );
}

function niceCeiling(value: number): number {
  if (value <= 0) return 1;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  return Math.ceil(value / magnitude) * magnitude;
}

/** Round tick values so the axis carries readable numbers, not raw maxima. */
function niceTicks(min: number, max: number, count: number): number[] {
  const step = (max - min) / count;
  if (!Number.isFinite(step) || step <= 0) return [min];
  const magnitude = 10 ** Math.floor(Math.log10(step));
  const normalised = [1, 2, 2.5, 5, 10].find((m) => m * magnitude >= step) ?? 10;
  const rounded = normalised * magnitude;
  const ticks: number[] = [];
  for (let value = Math.ceil(min / rounded) * rounded; value <= max; value += rounded) {
    ticks.push(Number(value.toFixed(6)));
  }
  return ticks;
}
