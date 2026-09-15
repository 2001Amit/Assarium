"use client";

import { useMemo, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { Play, ShieldAlert, X } from "lucide-react";
import { api, AssariumApiError } from "@/lib/api";
import { cn } from "@/lib/cn";
import type {
  MetricResult,
  SemanticModel,
  TimeGrain,
} from "@/lib/types";
import { Button } from "@/components/ui/Button";
import { Callout } from "@/components/ui/Callout";
import { Segmented } from "@/components/ui/Segmented";
import { formatMeasure } from "@/lib/measureFormat";

const GRAINS: TimeGrain[] = ["day", "week", "month", "quarter", "year"];

export interface QuerySelection {
  measures: string[];
  dimensions: string[];
  timeDimension: string | null;
  grain: TimeGrain;
}

export function QueryBuilder({
  connectionId,
  model,
  selection,
  onChange,
}: {
  connectionId: string;
  model: SemanticModel;
  selection: QuerySelection;
  onChange: (next: QuerySelection) => void;
}) {
  const [result, setResult] = useState<MetricResult | null>(null);
  const [refusal, setRefusal] = useState<string | null>(null);
  const [showSql, setShowSql] = useState(false);

  const measureById = useMemo(
    () => new Map(model.measures.map((measure) => [measure.id, measure])),
    [model.measures],
  );

  const run = useMutation({
    mutationFn: () =>
      api.post<MetricResult>(`/api/connections/${connectionId}/semantic/query`, {
        measures: selection.measures,
        dimensions: selection.dimensions,
        time_dimension: selection.timeDimension,
        time_grain: selection.timeDimension ? selection.grain : null,
        limit: 200,
      }),
    onMutate: () => {
      setRefusal(null);
      setResult(null);
    },
    onSuccess: setResult,
    onError: (error) =>
      setRefusal(
        error instanceof AssariumApiError ? error.message : "The query could not be run.",
      ),
  });

  const remove = (kind: "measures" | "dimensions", value: string) =>
    onChange({ ...selection, [kind]: selection[kind].filter((v) => v !== value) });

  const label = (reference: string) => {
    const [entityId, attributeName] = reference.split(".");
    const entity = model.entities.find((e) => e.id === entityId);
    const attribute = entity?.attributes.find((a) => a.name === attributeName);
    return attribute ? `${entity?.label} · ${attribute.label}` : reference;
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="shrink-0 border-b border-[var(--line)] bg-[var(--surface)] px-4 py-3">
        <div className="flex flex-wrap items-start gap-x-6 gap-y-3">
          <Slot title="Measures" empty="Pick at least one measure from the left.">
            {selection.measures.map((id) => (
              <Chip
                key={id}
                tone="accent"
                onRemove={() => remove("measures", id)}
                label={measureById.get(id)?.label ?? id}
              />
            ))}
          </Slot>

          <Slot title="Group by" empty="Optional. Nothing selected gives a single total.">
            {selection.timeDimension && (
              <Chip
                tone="neutral"
                label={`${label(selection.timeDimension)} by ${selection.grain}`}
                onRemove={() => onChange({ ...selection, timeDimension: null })}
              />
            )}
            {selection.dimensions.map((reference) => (
              <Chip
                key={reference}
                tone="neutral"
                label={label(reference)}
                onRemove={() => remove("dimensions", reference)}
              />
            ))}
          </Slot>

          {selection.timeDimension && (
            <div>
              <p className="mb-1.5 text-[10.5px] font-semibold uppercase tracking-[0.06em] text-[var(--text-subtle)]">
                Grain
              </p>
              <Segmented
                value={selection.grain}
                options={GRAINS.map((grain) => ({ value: grain, label: grain }))}
                onChange={(grain) => onChange({ ...selection, grain })}
              />
            </div>
          )}

          <div className="ml-auto flex items-center gap-2 self-end">
            {result && (
              <button
                onClick={() => setShowSql((value) => !value)}
                className="text-[11.5px] text-[var(--text-subtle)] underline underline-offset-2 hover:text-[var(--text)]"
              >
                {showSql ? "Hide" : "Show"} SQL
              </button>
            )}
            <Button
              size="sm"
              variant="primary"
              icon={Play}
              loading={run.isPending}
              disabled={selection.measures.length === 0}
              onClick={() => run.mutate()}
            >
              Run
            </Button>
          </div>
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-auto">
        {refusal && (
          <div className="p-4">
            <Callout tone="critical" title="This question cannot be answered from the model">
              {refusal}
            </Callout>
            <p className="mt-2 px-1 text-[11.5px] leading-relaxed text-[var(--text-subtle)]">
              The semantic layer refuses questions it cannot answer correctly rather than
              returning a number that looks plausible.
            </p>
          </div>
        )}

        {result?.notes.map((note) => (
          <div key={note} className="px-4 pt-3">
            <Callout tone="caution">
              <span className="flex items-start gap-1.5">
                <ShieldAlert className="mt-px size-3 shrink-0" aria-hidden />
                {note}
              </span>
            </Callout>
          </div>
        ))}

        {showSql && result && (
          <pre className="m-4 overflow-x-auto rounded-[var(--radius-md)] border border-[var(--line)] bg-[var(--surface-sunken)] px-3 py-2.5 font-mono text-[11px] leading-relaxed text-[var(--text-muted)]">
            {result.sql}
          </pre>
        )}

        {result && (
          <>
            <p className="px-4 pt-3 text-[11.5px] text-[var(--text-subtle)]">
              {result.row_count} {result.row_count === 1 ? "row" : "rows"} in{" "}
              {result.elapsed_ms} ms
              {result.truncated && " · truncated"}
            </p>
            <table className="mt-2 w-max min-w-full">
              <thead className="sticky top-0 bg-[var(--surface-sunken)]">
                <tr>
                  {result.columns.map((column) => (
                    <th
                      key={column}
                      className={cn(
                        "whitespace-nowrap border-y border-[var(--line)] px-3 py-1.5 font-mono text-[10.5px] font-medium text-[var(--text-subtle)]",
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
                      const measure = model.measures.find((m) => m.name === column);
                      return (
                        <td
                          key={cell}
                          className={cn(
                            "whitespace-nowrap border-b border-[var(--line)] px-3 py-1 text-[12px] tabular-nums",
                            isMeasure
                              ? "text-right font-medium text-[var(--text)]"
                              : "text-left text-[var(--text-muted)]",
                          )}
                        >
                          {value === null ? (
                            <span className="italic text-[var(--text-subtle)]">null</span>
                          ) : isMeasure && measure ? (
                            formatMeasure(value, measure)
                          ) : (
                            String(value).slice(0, 40)
                          )}
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}

        {!result && !refusal && (
          <p className="px-4 py-8 text-center text-[12.5px] text-[var(--text-subtle)]">
            Choose measures and dimensions, then run the query.
          </p>
        )}
      </div>
    </div>
  );
}

function Slot({
  title,
  empty,
  children,
}: {
  title: string;
  empty: string;
  children: React.ReactNode;
}) {
  const hasChildren = Array.isArray(children)
    ? children.flat().filter(Boolean).length > 0
    : Boolean(children);
  return (
    <div className="min-w-[180px]">
      <p className="mb-1.5 text-[10.5px] font-semibold uppercase tracking-[0.06em] text-[var(--text-subtle)]">
        {title}
      </p>
      <div className="flex flex-wrap gap-1.5">
        {hasChildren ? (
          children
        ) : (
          <span className="text-[11.5px] text-[var(--text-subtle)]">{empty}</span>
        )}
      </div>
    </div>
  );
}

function Chip({
  label,
  tone,
  onRemove,
}: {
  label: string;
  tone: "accent" | "neutral";
  onRemove: () => void;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-[var(--radius-sm)] border px-1.5 py-0.5 text-[11.5px]",
        tone === "accent"
          ? "border-[var(--accent)]/30 bg-[var(--accent-soft)] text-[var(--accent)]"
          : "border-[var(--line-strong)] text-[var(--text-muted)]",
      )}
    >
      {label}
      <button onClick={onRemove} aria-label={`Remove ${label}`} className="opacity-60 hover:opacity-100">
        <X className="size-3" aria-hidden />
      </button>
    </span>
  );
}
