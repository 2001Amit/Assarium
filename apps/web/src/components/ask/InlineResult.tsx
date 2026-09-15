"use client";

import { useState } from "react";
import { BarChart3, Code2, Table2, TrendingUp } from "lucide-react";
import { cn } from "@/lib/cn";
import type { ChatQueryResult } from "@/lib/types";

interface InlineResultProps {
  result: ChatQueryResult;
}

export function InlineResult({ result }: InlineResultProps) {
  const [showSql, setShowSql] = useState(false);

  if (result.rows.length === 0 && result.chart_hint !== "stat") {
    return (
      <p className="text-[11.5px] text-[var(--text-subtle)]">No data returned.</p>
    );
  }

  const ChartIcon =
    result.chart_hint === "bar"
      ? BarChart3
      : result.chart_hint === "line"
        ? TrendingUp
        : Table2;

  return (
    <div className="space-y-2">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-1.5 text-[10.5px] text-[var(--text-subtle)]">
          <ChartIcon className="size-3" aria-hidden />
          <span>
            {result.row_count} row{result.row_count !== 1 ? "s" : ""} · {result.elapsed_ms}ms
          </span>
        </div>
        {result.sql && (
          <button
            onClick={() => setShowSql((v) => !v)}
            className="flex items-center gap-1 text-[10.5px] text-[var(--text-subtle)] transition-colors hover:text-[var(--text)]"
          >
            <Code2 className="size-2.5" aria-hidden />
            {showSql ? "Hide SQL" : "SQL"}
          </button>
        )}
      </div>

      {/* SQL */}
      {showSql && result.sql && (
        <pre className="overflow-x-auto rounded-[var(--radius-sm)] bg-[var(--surface-sunken)] p-2 font-mono text-[10.5px] leading-relaxed text-[var(--text-muted)]">
          {result.sql}
        </pre>
      )}

      {/* Bar chart for bar hint */}
      {result.chart_hint === "bar" && result.dimension_columns.length > 0 && (
        <InlineBarChart result={result} />
      )}

      {/* Data table */}
      <div className="overflow-x-auto">
        <table className="w-full border-collapse text-[11.5px]">
          <thead>
            <tr>
              {result.columns.map((col, i) => (
                <th
                  key={i}
                  className={cn(
                    "border-b border-[var(--line)] px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.04em] text-[var(--text-subtle)]",
                    result.measure_columns.includes(col)
                      ? "text-right"
                      : "text-left",
                  )}
                >
                  {col.replace(/_/g, " ")}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {result.rows.slice(0, 20).map((row, rowIdx) => (
              <tr
                key={rowIdx}
                className="border-b border-[var(--line)] last:border-0"
              >
                {row.map((cell, colIdx) => {
                  const isMeasure = result.measure_columns.includes(
                    result.columns[colIdx],
                  );
                  return (
                    <td
                      key={colIdx}
                      className={cn(
                        "px-2 py-1 tabular-nums",
                        isMeasure
                          ? "text-right font-mono text-[var(--text)]"
                          : "text-[var(--text-muted)]",
                      )}
                    >
                      {_formatCell(cell)}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
        {result.rows.length > 20 && (
          <p className="mt-1 text-center text-[10px] text-[var(--text-subtle)]">
            Showing 20 of {result.rows.length} rows
          </p>
        )}
      </div>

      {/* Notes */}
      {result.notes.length > 0 && (
        <div className="space-y-0.5">
          {result.notes.map((note, i) => (
            <p key={i} className="text-[10.5px] text-[var(--text-subtle)]">
              {note}
            </p>
          ))}
        </div>
      )}
    </div>
  );
}

function InlineBarChart({ result }: { result: ChatQueryResult }) {
  const dimIndex = result.columns.indexOf(result.dimension_columns[0]);
  const measureCol = result.measure_columns[0];
  const measureIndex = result.columns.indexOf(measureCol);
  if (dimIndex < 0 || measureIndex < 0) return null;

  const items = result.rows.slice(0, 8).map((row) => ({
    label: String(row[dimIndex] ?? "—"),
    value: typeof row[measureIndex] === "number" ? (row[measureIndex] as number) : 0,
  }));

  const max = Math.max(...items.map((i) => Math.abs(i.value)), 1);

  return (
    <div className="space-y-[3px]">
      {items.map((item, i) => (
        <div key={i} className="flex items-center gap-2">
          <span className="w-[80px] shrink-0 truncate text-right text-[10.5px] text-[var(--text-muted)]">
            {item.label}
          </span>
          <div className="relative h-[14px] min-w-0 flex-1">
            <div
              className="h-full rounded-[2px] bg-[var(--accent)]"
              style={{
                width: `${Math.max((Math.abs(item.value) / max) * 100, 2)}%`,
                opacity: 0.6,
              }}
            />
          </div>
          <span className="w-[48px] shrink-0 text-right font-mono text-[10px] tabular-nums text-[var(--text-subtle)]">
            {_formatCell(item.value)}
          </span>
        </div>
      ))}
    </div>
  );
}

function _formatCell(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "number") {
    if (Math.abs(value) >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
    if (Math.abs(value) >= 1_000) return `${(value / 1_000).toFixed(1)}K`;
    if (Number.isInteger(value))
      return new Intl.NumberFormat("en").format(value);
    return value.toFixed(2);
  }
  const str = String(value);
  if (str.endsWith("T00:00:00") || str.endsWith(" 00:00:00")) return str.slice(0, 10);
  return str;
}
