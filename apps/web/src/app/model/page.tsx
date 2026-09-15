"use client";

import { Suspense, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Boxes,
  CalendarClock,
  ChevronDown,
  ChevronRight,
  KeyRound,
  Link2,
  RefreshCw,
  Sigma,
  ShieldAlert,
} from "lucide-react";
import { api, AssariumApiError } from "@/lib/api";
import { cn } from "@/lib/cn";
import { formatCount } from "@/lib/format";
import type { Connection, SemanticEntity, SemanticModel } from "@/lib/types";
import { PageHeader } from "@/components/PageHeader";
import { QueryBuilder, type QuerySelection } from "@/components/model/QueryBuilder";
import { Button } from "@/components/ui/Button";
import { Callout } from "@/components/ui/Callout";
import { EmptyState } from "@/components/ui/EmptyState";
import { inputClass } from "@/components/ui/Field";

const ROLE_ICON = {
  key: KeyRound,
  foreign_key: Link2,
  time: CalendarClock,
  dimension: null,
  attribute: null,
} as const;

function ModelWorkspace() {
  const params = useSearchParams();
  const queryClient = useQueryClient();
  const [connectionId, setConnectionId] = useState<string | null>(params.get("connection"));
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [selection, setSelection] = useState<QuerySelection>({
    measures: [],
    dimensions: [],
    timeDimension: null,
    grain: "month",
  });

  const { data: connections } = useQuery({
    queryKey: ["connections"],
    queryFn: () => api.get<Connection[]>("/api/connections"),
  });

  useEffect(() => {
    if (!connectionId && connections?.length) setConnectionId(connections[0].id);
  }, [connections, connectionId]);

  const { data: model, error } = useQuery({
    queryKey: ["semantic", connectionId],
    queryFn: () => api.get<SemanticModel>(`/api/connections/${connectionId}/semantic`),
    enabled: Boolean(connectionId),
    retry: false,
  });

  // Open the first fact entity so the page is useful on arrival.
  useEffect(() => {
    if (model && expanded.size === 0) {
      const fact = model.entities.find((entity) => entity.is_fact) ?? model.entities[0];
      if (fact) setExpanded(new Set([fact.id]));
    }
  }, [model, expanded.size]);

  const rebuild = useMutation({
    mutationFn: () =>
      api.post(`/api/connections/${connectionId}/semantic/build`, { overwrite_edits: true }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["semantic", connectionId] });
      queryClient.invalidateQueries({ queryKey: ["ontology", connectionId] });
      setSelection({ measures: [], dimensions: [], timeDimension: null, grain: "month" });
    },
  });

  const toggle = (id: string) =>
    setExpanded((previous) => {
      const next = new Set(previous);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const toggleMeasure = (id: string) =>
    setSelection((previous) => ({
      ...previous,
      measures: previous.measures.includes(id)
        ? previous.measures.filter((m) => m !== id)
        : [...previous.measures, id],
    }));

  const toggleDimension = (entity: SemanticEntity, name: string, isTime: boolean) => {
    const reference = `${entity.id}.${name}`;
    setSelection((previous) => {
      if (isTime) {
        return {
          ...previous,
          timeDimension: previous.timeDimension === reference ? null : reference,
        };
      }
      return {
        ...previous,
        dimensions: previous.dimensions.includes(reference)
          ? previous.dimensions.filter((d) => d !== reference)
          : [...previous.dimensions, reference],
      };
    });
  };

  const notFound = error instanceof AssariumApiError && error.status === 404;

  return (
    <>
      <PageHeader
        title="Semantic model"
        description="The definitions every dashboard and answer is built from."
        actions={
          <>
            <div className="relative">
              <select
                aria-label="Connection"
                value={connectionId ?? ""}
                onChange={(event) => setConnectionId(event.target.value)}
                className={cn(inputClass, "h-7 appearance-none py-0 pr-7 text-[12.5px]")}
              >
                {connections?.map((connection) => (
                  <option key={connection.id} value={connection.id}>
                    {connection.name}
                  </option>
                ))}
              </select>
              <ChevronDown
                className="pointer-events-none absolute right-2 top-1/2 size-3 -translate-y-1/2 text-[var(--text-subtle)]"
                aria-hidden
              />
            </div>
            <Button
              size="sm"
              icon={RefreshCw}
              loading={rebuild.isPending}
              onClick={() => rebuild.mutate()}
            >
              Rebuild
            </Button>
          </>
        }
      />

      {notFound ? (
        <EmptyState
          icon={Boxes}
          title="No semantic model yet"
          description="Build it from the silver layer to define what can be measured and how entities join."
          action={
            <Button variant="primary" loading={rebuild.isPending} onClick={() => rebuild.mutate()}>
              Build the model
            </Button>
          }
        />
      ) : (
        model && (
          <div className="flex min-h-0 flex-1">
            <aside className="flex w-[320px] shrink-0 flex-col border-r border-[var(--line)] bg-[var(--surface)]">
              <div className="min-h-0 flex-1 overflow-y-auto p-2">
                {model.entities.map((entity) => {
                  const isOpen = expanded.has(entity.id);
                  const measures = model.measures.filter((m) => m.entity_id === entity.id);
                  const groupable = entity.attributes.filter(
                    (a) => !a.hidden && (a.role === "dimension" || a.role === "time" || a.role === "key"),
                  );

                  return (
                    <div key={entity.id} className="mb-1">
                      <button
                        onClick={() => toggle(entity.id)}
                        className="flex w-full items-center gap-1.5 rounded-[var(--radius-sm)] px-1.5 py-1.5 text-left hover:bg-[var(--surface-hover)]"
                      >
                        <ChevronRight
                          className={cn(
                            "size-3 shrink-0 text-[var(--text-subtle)] transition-transform",
                            isOpen && "rotate-90",
                          )}
                          aria-hidden
                        />
                        <span className="min-w-0 flex-1 truncate text-[12.5px] font-medium text-[var(--text)]">
                          {entity.label}
                        </span>
                        {entity.is_fact && (
                          <span className="shrink-0 text-[10px] uppercase tracking-[0.05em] text-[var(--accent)]">
                            fact
                          </span>
                        )}
                        <span className="shrink-0 text-[10.5px] tabular-nums text-[var(--text-subtle)]">
                          {formatCount(entity.row_count)}
                        </span>
                      </button>

                      {isOpen && (
                        <div className="ml-4 border-l border-[var(--line)] pl-2">
                          <p className="mb-1 mt-1.5 flex items-center gap-1 text-[10px] font-semibold uppercase tracking-[0.06em] text-[var(--text-subtle)]">
                            <Sigma className="size-2.5" aria-hidden />
                            Measures
                          </p>
                          {measures.map((measure) => (
                            <button
                              key={measure.id}
                              onClick={() => toggleMeasure(measure.id)}
                              title={measure.description ?? undefined}
                              className={cn(
                                "flex w-full items-center gap-1.5 rounded-[var(--radius-sm)] px-1.5 py-1 text-left",
                                selection.measures.includes(measure.id)
                                  ? "bg-[var(--accent-soft)] text-[var(--accent)]"
                                  : "text-[var(--text-muted)] hover:bg-[var(--surface-hover)]",
                              )}
                            >
                              <span className="min-w-0 flex-1 truncate text-[11.5px]">
                                {measure.label}
                              </span>
                              <span className="shrink-0 font-mono text-[9.5px] uppercase text-[var(--text-subtle)]">
                                {measure.aggregation}
                              </span>
                            </button>
                          ))}

                          <p className="mb-1 mt-2 text-[10px] font-semibold uppercase tracking-[0.06em] text-[var(--text-subtle)]">
                            Group by
                          </p>
                          {groupable.map((attribute) => {
                            const reference = `${entity.id}.${attribute.name}`;
                            const isTime = attribute.role === "time";
                            const active = isTime
                              ? selection.timeDimension === reference
                              : selection.dimensions.includes(reference);
                            const Icon = ROLE_ICON[attribute.role];
                            return (
                              <button
                                key={attribute.name}
                                onClick={() => toggleDimension(entity, attribute.name, isTime)}
                                className={cn(
                                  "flex w-full items-center gap-1.5 rounded-[var(--radius-sm)] px-1.5 py-1 text-left",
                                  active
                                    ? "bg-[var(--accent-soft)] text-[var(--accent)]"
                                    : "text-[var(--text-muted)] hover:bg-[var(--surface-hover)]",
                                )}
                              >
                                {Icon ? (
                                  <Icon className="size-2.5 shrink-0" aria-hidden />
                                ) : (
                                  <span className="size-2.5 shrink-0" />
                                )}
                                <span className="min-w-0 flex-1 truncate text-[11.5px]">
                                  {attribute.label}
                                </span>
                                {attribute.contains_pii && (
                                  <ShieldAlert
                                    className="size-2.5 shrink-0 text-[var(--color-critical)]"
                                    aria-hidden
                                  />
                                )}
                                {attribute.cardinality !== null && (
                                  <span className="shrink-0 text-[9.5px] tabular-nums text-[var(--text-subtle)]">
                                    {formatCount(attribute.cardinality)}
                                  </span>
                                )}
                              </button>
                            );
                          })}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            </aside>

            <div className="flex min-w-0 flex-1 flex-col">
              {model.notes.length > 0 && (
                <div className="shrink-0 space-y-1.5 px-4 pt-3">
                  {model.notes.map((note) => (
                    <Callout key={note} tone="caution">
                      {note}
                    </Callout>
                  ))}
                </div>
              )}
              {connectionId && (
                <QueryBuilder
                  connectionId={connectionId}
                  model={model}
                  selection={selection}
                  onChange={setSelection}
                />
              )}
            </div>
          </div>
        )
      )}
    </>
  );
}

export default function ModelPage() {
  return (
    <Suspense fallback={<PageHeader title="Semantic model" />}>
      <ModelWorkspace />
    </Suspense>
  );
}
