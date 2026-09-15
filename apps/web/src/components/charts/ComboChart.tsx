"use client";

import { useMemo, useRef, useState } from "react";
import { formatAxis, formatMeasure } from "@/lib/measureFormat";
import { seriesColor } from "@/lib/palette";
import type { SemanticMeasure } from "@/lib/types";

export interface ComboPoint {
  label: string;
  values: Record<string, number | null>;
}

const PADDING = { top: 16, right: 56, bottom: 26, left: 56 };

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
const LEGEND_HEIGHT = 22;

/**
 * Bars on the left axis, a line on the right.
 *
 * The only honest reason to put two measures on one chart with two scales is that the
 * *relationship* between them is the subject - revenue against occupancy, spend against
 * conversion. Two measures of the same unit get one axis or two charts, because a second
 * scale would manufacture a correlation the data does not contain. The generator only
 * proposes this pairing when the units genuinely differ.
 *
 * The two axes are labelled and coloured to match their series, so nobody has to guess
 * which number belongs to which side - the usual failure of a dual-axis chart.
 */
export function ComboChart({
  points,
  barMeasures,
  lineMeasures,
  width,
  height,
}: {
  points: ComboPoint[];
  barMeasures: SemanticMeasure[];
  lineMeasures: SemanticMeasure[];
  width: number;
  height: number;
}) {
  const svgRef = useRef<SVGSVGElement>(null);
  const [active, setActive] = useState<number | null>(null);

  // The SVG gets what is left after the legend, never the whole box.
  const svgHeight = Math.max(60, height - LEGEND_HEIGHT);
  const plotWidth = Math.max(10, width - PADDING.left - PADDING.right);
  const plotHeight = Math.max(10, svgHeight - PADDING.top - PADDING.bottom);

  const scales = useMemo(() => {
    const collect = (measures: SemanticMeasure[]) => {
      const values: number[] = [];
      for (const point of points) {
        for (const measure of measures) {
          const value = point.values[measure.name] ?? point.values[measure.id];
          if (typeof value === "number") values.push(value);
        }
      }
      // Bars are read as lengths, so their baseline has to be zero. A line is read as a
      // shape, so its baseline may float - but never above zero for positive data.
      const lo = Math.min(0, ...values);
      const hi = niceCeiling(Math.max(...values, 0));
      return { lo, hi, span: hi - lo || 1 };
    };
    return { left: collect(barMeasures), right: collect(lineMeasures) };
  }, [points, barMeasures, lineMeasures]);

  if (points.length === 0) {
    return (
      <p className="flex h-full items-center justify-center text-[12px] text-[var(--text-subtle)]">
        No rows match the current filters.
      </p>
    );
  }

  const band = plotWidth / points.length;
  const barWidth = Math.max(4, Math.min(28, band * 0.5));

  const yLeft = (value: number) =>
    PADDING.top + plotHeight - ((value - scales.left.lo) / scales.left.span) * plotHeight;
  const yRight = (value: number) =>
    PADDING.top + plotHeight - ((value - scales.right.lo) / scales.right.span) * plotHeight;
  const xAt = (index: number) => PADDING.left + band * index + band / 2;

  const valueOf = (point: ComboPoint, measure: SemanticMeasure) =>
    point.values[measure.name] ?? point.values[measure.id] ?? null;

  const linePath = (measure: SemanticMeasure) =>
    points
      .map((point, index) => {
        const value = valueOf(point, measure);
        if (value === null) return null;
        return `${index === 0 ? "M" : "L"} ${xAt(index)} ${yRight(value)}`;
      })
      .filter(Boolean)
      .join(" ");

  const leftColour = seriesColor(0);
  const rightColour = seriesColor(2);

  return (
    <div className="flex h-full flex-col overflow-hidden">
      <svg
        ref={svgRef}
        width={width}
        height={svgHeight}
        role="img"
        aria-label="Two measures on separate axes"
        onMouseLeave={() => setActive(null)}
        onMouseMove={(event) => {
          const box = svgRef.current?.getBoundingClientRect();
          if (!box) return;
          const index = Math.floor((event.clientX - box.left - PADDING.left) / band);
          setActive(index >= 0 && index < points.length ? index : null);
        }}
      >
        {[0, 0.25, 0.5, 0.75, 1].map((fraction) => {
          const y = PADDING.top + plotHeight * fraction;
          return (
            <g key={fraction}>
              <line
                x1={PADDING.left}
                x2={PADDING.left + plotWidth}
                y1={y}
                y2={y}
                stroke="var(--border-subtle)"
                strokeWidth={1}
              />
              <text
                x={PADDING.left - 8}
                y={y + 3}
                textAnchor="end"
                fill={leftColour}
                className="text-[10px] tabular-nums"
              >
                {formatAxis(scales.left.hi - scales.left.span * fraction, barMeasures[0])}
              </text>
              <text
                x={PADDING.left + plotWidth + 8}
                y={y + 3}
                fill={rightColour}
                className="text-[10px] tabular-nums"
              >
                {formatAxis(scales.right.hi - scales.right.span * fraction, lineMeasures[0])}
              </text>
            </g>
          );
        })}

        {points.map((point, index) => {
          const value = valueOf(point, barMeasures[0]);
          if (value === null) return null;
          const top = yLeft(value);
          return (
            <rect
              key={point.label}
              x={xAt(index) - barWidth / 2}
              y={Math.min(top, yLeft(0))}
              width={barWidth}
              height={Math.max(1, Math.abs(yLeft(0) - top))}
              rx={2}
              fill={leftColour}
              opacity={active === null || active === index ? 0.9 : 0.4}
            />
          );
        })}

        {lineMeasures.map((measure, index) => (
          <path
            key={measure.id}
            d={linePath(measure)}
            fill="none"
            stroke={seriesColor(index + 2)}
            strokeWidth={2}
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        ))}

        {active !== null && (
          <line
            x1={xAt(active)}
            x2={xAt(active)}
            y1={PADDING.top}
            y2={PADDING.top + plotHeight}
            stroke="var(--border)"
            strokeWidth={1}
          />
        )}

        {points.map((point, index) =>
          index % Math.ceil(points.length / 6) === 0 ? (
            <text
              key={`x-${point.label}`}
              x={xAt(index)}
              y={svgHeight - 8}
              textAnchor="middle"
              fill="var(--text-subtle)"
              className="text-[10px]"
            >
              {point.label}
            </text>
          ) : null,
        )}
      </svg>

      {/* Fixed height and no wrapping, so the legend can never push the tile taller
          than the box it was measured in. */}
      <div
        className="flex shrink-0 items-center gap-x-4 overflow-hidden whitespace-nowrap px-1 text-[11px]"
        style={{ height: LEGEND_HEIGHT }}
      >
        {barMeasures.slice(0, 1).map((measure) => (
          <span key={measure.id} className="flex items-center gap-1.5 text-[var(--text-subtle)]">
            <span className="h-2.5 w-2.5 rounded-[2px]" style={{ background: leftColour }} />
            {measure.label}
            {active !== null && (
              <span className="tabular-nums text-[var(--text)]">
                {formatMeasure(valueOf(points[active], measure), measure)}
              </span>
            )}
          </span>
        ))}
        {lineMeasures.map((measure, index) => (
          <span key={measure.id} className="flex items-center gap-1.5 text-[var(--text-subtle)]">
            <span
              className="h-0.5 w-3.5 rounded-full"
              style={{ background: seriesColor(index + 2) }}
            />
            {measure.label}
            {active !== null && (
              <span className="tabular-nums text-[var(--text)]">
                {formatMeasure(valueOf(points[active], measure), measure)}
              </span>
            )}
          </span>
        ))}
      </div>
    </div>
  );
}

/** Round an axis maximum up to something a person would have chosen. */
function niceCeiling(value: number): number {
  if (value <= 0) return 1;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  const normalised = value / magnitude;
  const step = normalised <= 1 ? 1 : normalised <= 2 ? 2 : normalised <= 5 ? 5 : 10;
  return step * magnitude;
}
