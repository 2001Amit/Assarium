"use client";

import { Suspense, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { ChevronDown, GitBranch } from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import type { Connection, LineageGraph, LineageNode, LineageTrace } from "@/lib/types";
import { PageHeader } from "@/components/PageHeader";
import { LineageBoard } from "@/components/lineage/LineageBoard";
import { Callout } from "@/components/ui/Callout";
import { EmptyState } from "@/components/ui/EmptyState";
import { inputClass } from "@/components/ui/Field";
import { Segmented } from "@/components/ui/Segmented";

type Direction = "upstream" | "downstream";

function LineageWorkspace() {
  const params = useSearchParams();
  const [connectionId, setConnectionId] = useState<string | null>(params.get("connection"));
  const [selected, setSelected] = useState<LineageNode | null>(null);
  const [direction, setDirection] = useState<Direction>("upstream");

  const { data: connections } = useQuery({
    queryKey: ["connections"],
    queryFn: () => api.get<Connection[]>("/api/connections"),
  });

  useEffect(() => {
    if (!connectionId && connections?.length) setConnectionId(connections[0].id);
  }, [connections, connectionId]);

  const { data: graph, isLoading } = useQuery({
    queryKey: ["lineage", connectionId],
    queryFn: () => api.get<LineageGraph>(`/api/connections/${connectionId}/lineage`),
    enabled: Boolean(connectionId),
    retry: false,
  });

  const { data: trace } = useQuery({
    queryKey: ["lineage-trace", connectionId, selected?.id, direction],
    queryFn: () => {
      const query = new URLSearchParams({
        kind: selected!.kind,
        table: selected!.table,
        column: selected!.column,
      });
      const path = direction === "upstream" ? "provenance" : "impact";
      return api.get<LineageTrace>(
        `/api/connections/${connectionId}/lineage/${path}?${query}`,
      );
    },
    enabled: Boolean(connectionId && selected),
    retry: false,
  });

  // Everything the selection touches, so the board can dim the rest. Both directions are
  // included regardless of which panel is showing: a reader following a column wants to
  // see its whole path, not the half they happen to have selected.
  const related = useMemo(() => {
    const ids = new Set<string>();
    if (!selected || !graph) return ids;
    const forward = new Map<string, string[]>();
    const back = new Map<string, string[]>();
    for (const edge of graph.edges) {
      forward.set(edge.source.id, [...(forward.get(edge.source.id) ?? []), edge.target.id]);
      back.set(edge.target.id, [...(back.get(edge.target.id) ?? []), edge.source.id]);
    }
    for (const index of [forward, back]) {
      const queue = [selected.id];
      while (queue.length) {
        const current = queue.pop()!;
        for (const next of index.get(current) ?? []) {
          if (!ids.has(next)) {
            ids.add(next);
            queue.push(next);
          }
        }
      }
    }
    return ids;
  }, [selected, graph]);

  const coverage = graph?.coverage;

  return (
    <>
      <PageHeader
        title="Lineage"
        description="Where every number came from, and what changes if a column moves."
        actions={
          <div className="relative">
            <select
              aria-label="Connection"
              className={cn(inputClass, "appearance-none pr-8")}
              value={connectionId ?? ""}
              onChange={(event) => {
                setConnectionId(event.target.value);
                setSelected(null);
              }}
            >
              {(connections ?? []).map((connection) => (
                <option key={connection.id} value={connection.id}>
                  {connection.name}
                </option>
              ))}
            </select>
            <ChevronDown
              className="pointer-events-none absolute right-2 top-1/2 size-3.5 -translate-y-1/2 text-[var(--text-subtle)]"
              aria-hidden
            />
          </div>
        }
      />

      {coverage && !coverage.complete && (
        <div className="px-4 pt-3">
          <Callout tone="caution" title="This graph is incomplete">
            <ul className="space-y-0.5">
              {coverage.unresolved_steps.map((step) => (
                <li key={step.step}>
                  <span className="text-[var(--text)]">{step.step}</span> — {step.reason}
                </li>
              ))}
            </ul>
          </Callout>
        </div>
      )}

      <div className="flex min-h-0 flex-1 gap-3 p-4">
        <section className="surface-panel flex min-h-0 flex-[3] flex-col overflow-hidden">
          {isLoading ? (
            <p className="p-6 text-[12.5px] text-[var(--text-subtle)]">Tracing…</p>
          ) : (
            <LineageBoard
              edges={graph?.edges ?? []}
              selected={selected?.id ?? null}
              related={related}
              onSelect={(node) =>
                setSelected((current) => (current?.id === node.id ? null : node))
              }
            />
          )}
        </section>

        <aside className="surface-panel flex min-h-0 w-[320px] shrink-0 flex-col overflow-hidden">
          {selected ? (
            <>
              <header className="shrink-0 border-b border-[var(--line)] px-3 py-2">
                <h2 className="truncate text-[12.5px] font-medium text-[var(--text)]">
                  {selected.column}
                </h2>
                <p className="truncate text-[11px] text-[var(--text-subtle)]">
                  {selected.kind} · {selected.table}
                </p>
                <div className="mt-2">
                  <Segmented<Direction>
                    value={direction}
                    onChange={setDirection}
                    options={[
                      { value: "upstream", label: "Came from" },
                      { value: "downstream", label: "Affects" },
                    ]}
                  />
                </div>
              </header>

              <div className="min-h-0 flex-1 overflow-y-auto p-3">
                {direction === "upstream" ? (
                  <Summary
                    title="Origins"
                    empty="This is where the data enters the platform."
                    items={(trace?.origins ?? []).map((o) => o.replace("source:", ""))}
                  />
                ) : (
                  <>
                    {/* Dashboards first: a silver column changing is a fact about the
                        pipeline, a tile going blank is a fact about somebody's morning. */}
                    <Summary
                      title="Dashboard tiles affected"
                      empty="No dashboard reads this."
                      items={trace?.tiles ?? []}
                    />
                    <Summary
                      title="Measures affected"
                      empty="No governed measure uses it."
                      items={trace?.measures ?? []}
                    />
                    <Summary
                      title="Tables affected"
                      empty="Nothing downstream."
                      items={trace?.tables ?? []}
                    />
                  </>
                )}

                <h3 className="mb-1 mt-4 text-[11px] font-medium uppercase tracking-wide text-[var(--text-subtle)]">
                  Steps
                </h3>
                <ol className="space-y-1.5">
                  {(trace?.edges ?? []).map((edge, index) => (
                    <li
                      key={`${edge.source.id}->${edge.target.id}-${index}`}
                      className="rounded-[var(--radius-sm)] border border-[var(--line)] px-2 py-1.5"
                    >
                      <p className="truncate text-[11.5px] text-[var(--text)]">
                        {edge.source.table}.{edge.source.column}
                        {" → "}
                        {edge.target.column}
                      </p>
                      {edge.transform && (
                        <code className="mt-1 block break-all font-mono text-[10px] text-[var(--text-muted)]">
                          {edge.transform}
                        </code>
                      )}
                    </li>
                  ))}
                </ol>
              </div>
            </>
          ) : (
            <EmptyState
              icon={GitBranch}
              title="Pick a column"
              description="Choose anything on the left to see where it came from, or what would break if it changed."
            />
          )}
        </aside>
      </div>
    </>
  );
}

function Summary({
  title,
  items,
  empty,
}: {
  title: string;
  items: string[];
  empty: string;
}) {
  return (
    <div className="mb-3">
      <h3 className="mb-1 text-[11px] font-medium uppercase tracking-wide text-[var(--text-subtle)]">
        {title}
      </h3>
      {items.length === 0 ? (
        <p className="text-[11.5px] text-[var(--text-subtle)]">{empty}</p>
      ) : (
        <ul className="space-y-0.5">
          {items.map((item) => (
            <li key={item} className="truncate text-[11.5px] text-[var(--text)]">
              {item}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export default function LineagePage() {
  return (
    <Suspense fallback={null}>
      <LineageWorkspace />
    </Suspense>
  );
}
