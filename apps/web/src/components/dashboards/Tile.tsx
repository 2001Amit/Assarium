"use client";

import { useEffect, useRef, useState } from "react";
import {
  ArrowDownRight,
  ArrowRight,
  ArrowUpRight,
  Code2,
  Download,
  Sparkles,
  Table2,
  TriangleAlert,
} from "lucide-react";
import { cn } from "@/lib/cn";
import { formatMeasure } from "@/lib/measureFormat";
import type { SemanticMeasure, TileResult } from "@/lib/types";
import { BarChart, type BarDatum } from "@/components/charts/BarChart";
import { ComboChart } from "@/components/charts/ComboChart";
import { DonutChart } from "@/components/charts/DonutChart";
import { LineChart } from "@/components/charts/LineChart";
import { ScatterChart } from "@/components/charts/ScatterChart";
import { StackedBarChart } from "@/components/charts/StackedBarChart";
import { Sparkline } from "@/components/charts/Sparkline";

export function Tile({
  result,
  measures,
  drillPath,
  activeDimension,
  crossFilterValue,
  onCrossFilter,
  onDrill,
  onExplain,
  onExport,
  explanation,
  explaining,
}: {
  result: TileResult;
  measures: SemanticMeasure[];
  drillPath?: string[];
  activeDimension?: string | null;
  crossFilterValue?: string | null;
  onCrossFilter?: (label: string, raw: unknown) => void;
  onDrill?: (dimension: string) => void;
  onExplain?: () => void;
  onExport?: () => void;
  explanation?: string | null;
  explaining?: boolean;
}) {
  const [view, setView] = useState<"chart" | "table" | "sql">("chart");
  const bodyRef = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ width: 400, height: 200 });

  useEffect(() => {
    const element = bodyRef.current;
    if (!element) return;
    const observer = new ResizeObserver(([entry]) => {
      const { width, height } = entry.contentRect;
      if (width > 0 && height > 0) setSize({ width, height });
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  /**
   * Resolve an output column to the measure that produced it.
   *
   * By id where the server told us one, because column names are not unique across the
   * model - two entities can each have a `record_count`, and matching on the name alone
   * labels the chart with whichever happens to come first. The name fallbacks are for
   * results from before ids were carried.
   */
  const measureFor = (name: string) => {
    const index = result.measure_columns.indexOf(name);
    const id = index >= 0 ? result.measure_ids?.[index] : undefined;
    return (
      (id ? measures.find((m) => m.id === id) : undefined) ??
      measures.find((m) => m.name === name) ??
      measures.find((m) => m.id.endsWith(`.${name}`))
    );
  };

  const primaryMeasure = measureFor(result.measure_columns[0] ?? "");

  // A combo tile splits its measures across two axes. The split is decided server-side
  // and carried on the result, so the renderer never has to guess which measure belongs
  // where - guessing is how a currency ends up on a percentage axis.
  const secondary = new Set(result.secondary_measure_columns ?? []);
  const barMeasures = result.measure_columns
    .filter((name) => !secondary.has(name))
    .map(measureFor)
    .filter((m): m is SemanticMeasure => Boolean(m));
  const lineMeasures = result.measure_columns
    .filter((name) => secondary.has(name))
    .map(measureFor)
    .filter((m): m is SemanticMeasure => Boolean(m));

  return (
    <section className="surface-panel flex min-h-0 flex-col overflow-hidden">
      <header className="flex shrink-0 items-start gap-2 border-b border-[var(--line)] px-3 py-2">
        <div className="min-w-0 flex-1">
          <h3 className="truncate text-[12.5px] font-medium text-[var(--text)]">
            {result.title}
          </h3>
          {result.notes.length > 0 && (
            <p className="mt-0.5 flex items-start gap-1 text-[10.5px] leading-snug text-[var(--color-caution)]">
              <TriangleAlert className="mt-px size-2.5 shrink-0" aria-hidden />
              {result.notes[0]}
            </p>
          )}
        </div>

        <div className="flex shrink-0 items-center gap-0.5">
          {onExplain && (
            <IconButton
              icon={Sparkles}
              label="Explain this"
              onClick={onExplain}
              busy={explaining}
              accent
            />
          )}
          {result.type !== "stat" && (
            <>
              <IconButton
                icon={Table2}
                label={view === "table" ? "Show chart" : "Show as table"}
                active={view === "table"}
                onClick={() => setView(view === "table" ? "chart" : "table")}
              />
              <IconButton
                icon={Code2}
                label={view === "sql" ? "Hide SQL" : "Show SQL"}
                active={view === "sql"}
                onClick={() => setView(view === "sql" ? "chart" : "sql")}
              />
            </>
          )}
          {onExport && <IconButton icon={Download} label="Download CSV" onClick={onExport} />}
        </div>
      </header>

      {drillPath && drillPath.length > 0 && onDrill && view === "chart" && (
        <div className="flex shrink-0 flex-wrap items-center gap-1 border-b border-[var(--line)] px-3 py-1.5">
          <span className="text-[10px] uppercase tracking-[0.05em] text-[var(--text-subtle)]">
            Break down by
          </span>
          {[activeDimension, ...drillPath].filter(Boolean).map((dimension) => (
            <button
              key={dimension}
              onClick={() => onDrill(dimension as string)}
              className={cn(
                "rounded-[var(--radius-xs)] px-1.5 py-px text-[10.5px] transition-colors",
                dimension === activeDimension
                  ? "bg-[var(--accent-soft)] text-[var(--accent)]"
                  : "text-[var(--text-muted)] hover:bg-[var(--surface-hover)]",
              )}
            >
              {(dimension as string).split(".").pop()}
            </button>
          ))}
        </div>
      )}

      <div ref={bodyRef} className="min-h-0 flex-1 overflow-hidden p-3">
        {result.error ? (
          <div className="flex h-full items-center">
            <p className="text-[12px] leading-relaxed text-[var(--color-critical)]">
              {result.error}
            </p>
          </div>
        ) : view === "sql" ? (
          <pre className="h-full overflow-auto rounded-[var(--radius-sm)] bg-[var(--surface-sunken)] p-2 font-mono text-[10.5px] leading-relaxed text-[var(--text-muted)]">
            {result.sql.join("\n\n")}
          </pre>
        ) : view === "table" || result.type === "table" ? (
          <TableView result={result} measureFor={measureFor} />
        ) : result.type === "stat" ? (
          <StatView result={result} measure={primaryMeasure} />
        ) : result.type === "line" || result.type === "area" ? (
          <LineChart
            points={result.points.map((p) => ({
              label: p.label,
              value: p.values[result.measure_columns[0]] ?? null,
            }))}
            measure={primaryMeasure}
            width={size.width}
            height={size.height}
            filled={result.type === "area"}
          />
        ) : result.type === "donut" ? (
          <DonutChart
            data={result.points.map((p) => ({
              label: p.label,
              value: p.values[result.measure_columns[0]] ?? null,
              raw: p.raw,
            }))}
            measure={primaryMeasure}
            size={Math.min(size.height, size.width * 0.5)}
            width={size.width}
            selected={crossFilterValue ?? null}
            onSelect={onCrossFilter ? (s) => onCrossFilter(s.label, s.raw) : undefined}
          />
        ) : result.type === "scatter" ? (
          <ScatterChart
            points={result.points.map((p) => ({
              label: p.label,
              x: p.values[result.measure_columns[0]] ?? null,
              y: p.values[result.measure_columns[1]] ?? null,
              raw: p.raw,
            }))}
            xMeasure={measureFor(result.measure_columns[0])}
            yMeasure={measureFor(result.measure_columns[1])}
            width={size.width}
            height={size.height}
            selected={crossFilterValue ?? null}
            onSelect={onCrossFilter ? (p) => onCrossFilter(p.label, p.raw) : undefined}
          />
        ) : result.type === "combo" ? (
          <ComboChart
            points={result.points.map((p) => ({ label: p.label, values: p.values }))}
            barMeasures={barMeasures}
            lineMeasures={lineMeasures}
            width={size.width}
            height={size.height}
          />
        ) : result.type === "stacked_bar" ? (
          <StackedBarChart
            rows={result.points.map((p) => ({
              label: p.label,
              values: p.values,
              raw: p.raw,
            }))}
            measures={result.measure_columns
              .map(measureFor)
              .filter((m): m is SemanticMeasure => Boolean(m))}
            width={size.width}
            height={size.height}
            selected={crossFilterValue ?? null}
            onSelect={onCrossFilter ? (r) => onCrossFilter(r.label, r.raw) : undefined}
          />
        ) : (
          <BarChart
            data={result.points.map<BarDatum>((p) => ({
              label: p.label,
              value: p.values[result.measure_columns[0]] ?? null,
              raw: p.raw,
            }))}
            measure={primaryMeasure}
            height={size.height}
            selected={crossFilterValue ?? null}
            onSelect={
              onCrossFilter ? (datum) => onCrossFilter(datum.label, datum.raw) : undefined
            }
          />
        )}
      </div>

      {explanation && (
        <div className="shrink-0 border-t border-[var(--line)] bg-[var(--accent-soft)]/40 px-3 py-2">
          <p className="flex items-start gap-1.5 text-[11.5px] leading-relaxed text-[var(--text-muted)]">
            <Sparkles className="mt-0.5 size-3 shrink-0 text-[var(--accent)]" aria-hidden />
            <span>{explanation}</span>
          </p>
        </div>
      )}
    </section>
  );
}

function StatView({
  result,
  measure,
}: {
  result: TileResult;
  measure: SemanticMeasure | undefined;
}) {
  const stat = result.stats[0];
  if (!stat) return null;

  const Arrow =
    stat.direction === "up" ? ArrowUpRight : stat.direction === "down" ? ArrowDownRight : ArrowRight;

  return (
    <div className="flex h-full flex-col justify-between gap-2">
      <div>
        {/* Hero figure: the number is the chart. */}
        <p className="text-[26px] font-semibold leading-none tracking-[-0.02em] tabular-nums text-[var(--text)]">
          {measure ? formatMeasure(stat.value, measure) : String(stat.value ?? "—")}
        </p>
        {stat.delta !== null && stat.delta !== undefined && (
          <p className="mt-1.5 flex items-center gap-1 text-[11.5px] text-[var(--text-muted)]">
            {/* Direction is stated, not judged: whether up is good depends on the measure. */}
            <Arrow className="size-3 shrink-0" aria-hidden />
            <span className="font-medium tabular-nums text-[var(--text)]">
              {stat.delta_pct !== null && stat.delta_pct !== undefined
                ? `${stat.delta_pct >= 0 ? "+" : ""}${(stat.delta_pct * 100).toFixed(1)}%`
                : measure
                  ? formatMeasure(stat.delta, measure)
                  : stat.delta}
            </span>
            <span className="truncate">{stat.period_label}</span>
          </p>
        )}
      </div>
      {stat.sparkline.length > 1 && (
        <div className="self-end">
          <Sparkline values={stat.sparkline} />
        </div>
      )}
    </div>
  );
}

function TableView({
  result,
  measureFor,
}: {
  result: TileResult;
  measureFor: (name: string) => SemanticMeasure | undefined;
}) {
  if (result.type === "stat") {
    return (
      <table className="w-full">
        <tbody>
          {result.stats.map((stat) => (
            <tr key={stat.measure}>
              <td className="py-1 text-[12px] text-[var(--text-muted)]">{stat.measure}</td>
              <td className="py-1 text-right text-[12px] tabular-nums">{stat.value}</td>
            </tr>
          ))}
        </tbody>
      </table>
    );
  }

  // Per-column extents for the in-cell bars. Scaled within a column, never across the
  // table: two columns in different units share no scale, and a bar that implies they do
  // is worse than no bar. Columns with negative values are skipped rather than drawn from
  // a shifted baseline, which reads as "small" when it means "below zero".
  const extents = new Map<string, number>();
  for (const column of result.measure_columns) {
    const index = result.columns.indexOf(column);
    if (index < 0) continue;
    const values = result.rows
      .map((row) => row[index])
      .filter((value): value is number => typeof value === "number");
    if (values.length === 0 || Math.min(...values) < 0) continue;
    const max = Math.max(...values);
    if (max > 0) extents.set(column, max);
  }

  return (
    <div className="h-full overflow-auto">
      <table className="w-max min-w-full">
        <thead className="sticky top-0 bg-[var(--surface)]">
          <tr>
            {result.columns.map((column) => (
              <th
                key={column}
                className={cn(
                  "whitespace-nowrap border-b border-[var(--line)] px-2 py-1 font-mono text-[10px] font-medium text-[var(--text-subtle)]",
                  result.measure_columns.includes(column) ? "text-right" : "text-left",
                )}
              >
                {column}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {result.rows.map((row, index) => (
            <tr key={index} className="hover:bg-[var(--surface-hover)]">
              {row.map((value, cell) => {
                const column = result.columns[cell];
                const isMeasure = result.measure_columns.includes(column);
                const measure = isMeasure ? measureFor(column) : undefined;
                return (
                  <td
                    key={cell}
                    className={cn(
                      "relative whitespace-nowrap border-b border-[var(--line)] px-2 py-1 text-[11.5px] tabular-nums",
                      isMeasure
                        ? "text-right font-medium text-[var(--text)]"
                        : "text-left text-[var(--text-muted)]",
                    )}
                  >
                    {/* A length behind the number, so a column of figures can be scanned
                        for shape as well as read. Kept faint and behind the text: it is
                        an aid to the number, not a replacement for it. */}
                    {isMeasure &&
                      typeof value === "number" &&
                      extents.has(column) && (
                        <span
                          aria-hidden
                          className="absolute inset-y-[3px] right-0 rounded-l-[2px]"
                          style={{
                            width: `${Math.max(1, (value / (extents.get(column) as number)) * 100)}%`,
                            background: "var(--series-1)",
                            opacity: 0.14,
                          }}
                        />
                      )}
                    <span className="relative">
                      {value === null ? (
                        <span className="italic text-[var(--text-subtle)]">null</span>
                      ) : measure ? (
                        formatMeasure(value, measure)
                      ) : (
                        String(value).slice(0, 40)
                      )}
                    </span>
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function IconButton({
  icon: Icon,
  label,
  onClick,
  active,
  busy,
  accent,
}: {
  icon: React.ComponentType<{ className?: string }>;
  label: string;
  onClick: () => void;
  active?: boolean;
  busy?: boolean;
  accent?: boolean;
}) {
  return (
    <button
      onClick={onClick}
      title={label}
      aria-label={label}
      disabled={busy}
      className={cn(
        "rounded-[var(--radius-sm)] p-1 transition-colors",
        active
          ? "bg-[var(--accent-soft)] text-[var(--accent)]"
          : accent
            ? "text-[var(--accent)] hover:bg-[var(--accent-soft)]"
            : "text-[var(--text-subtle)] hover:bg-[var(--surface-hover)] hover:text-[var(--text)]",
        busy && "animate-pulse",
      )}
    >
      <Icon className="size-3.5" aria-hidden />
    </button>
  );
}
