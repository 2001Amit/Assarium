"use client";

import { useMemo } from "react";
import { ArrowRight, CircleDot } from "lucide-react";
import { cn } from "@/lib/cn";
import type { LineageEdge, LineageNode } from "@/lib/types";

/**
 * Lineage drawn as ordered layers rather than a free graph.
 *
 * A force-directed graph is the reflex here and it is the wrong shape for this data.
 * Lineage in a medallion pipeline is inherently ordered — source, bronze, silver, gold,
 * measure, tile — and that order is the thing the reader is trying to follow. Laying it
 * out in columns makes the direction of flow unambiguous and makes "which layer did the
 * value change in" answerable at a glance, neither of which survives a spaghetti of
 * crossing edges.
 *
 * Selecting a node dims everything it is not connected to. That is the whole interaction:
 * a lineage view is only useful when it can answer one column's question at a time, and
 * a graph showing every edge at once answers nobody's.
 */

const LAYERS: { kind: string; label: string; hint: string }[] = [
  { kind: "source", label: "Source", hint: "As it arrived" },
  { kind: "bronze", label: "Bronze", hint: "Landed unchanged" },
  { kind: "silver", label: "Silver", hint: "Cleaned and typed" },
  { kind: "gold", label: "Gold", hint: "Business grain" },
  { kind: "measure", label: "Measure", hint: "Governed definition" },
  { kind: "tile", label: "Dashboard", hint: "What people read" },
];

export function LineageBoard({
  edges,
  selected,
  onSelect,
  related,
}: {
  edges: LineageEdge[];
  selected: string | null;
  onSelect: (node: LineageNode) => void;
  /** Node ids connected to the selection, in either direction. */
  related: Set<string>;
}) {
  const byLayer = useMemo(() => {
    const nodes = new Map<string, LineageNode>();
    for (const edge of edges) {
      nodes.set(edge.source.id, edge.source);
      nodes.set(edge.target.id, edge.target);
    }
    const grouped = new Map<string, LineageNode[]>();
    for (const node of nodes.values()) {
      const list = grouped.get(node.kind) ?? [];
      list.push(node);
      grouped.set(node.kind, list);
    }
    for (const list of grouped.values()) {
      list.sort((a, b) =>
        a.table === b.table
          ? a.column.localeCompare(b.column)
          : a.table.localeCompare(b.table),
      );
    }
    return grouped;
  }, [edges]);

  const transformOf = useMemo(() => {
    const map = new Map<string, string>();
    for (const edge of edges) {
      if (edge.transform) map.set(edge.target.id, edge.transform);
    }
    return map;
  }, [edges]);

  const present = LAYERS.filter((layer) => (byLayer.get(layer.kind) ?? []).length > 0);

  if (present.length === 0) {
    return (
      <p className="p-6 text-[12.5px] text-[var(--text-subtle)]">
        Nothing to trace yet. Run the pipeline, then build the semantic model.
      </p>
    );
  }

  return (
    <div className="flex min-h-0 flex-1 gap-3 overflow-x-auto p-3">
      {present.map((layer, index) => {
        const nodes = byLayer.get(layer.kind) ?? [];
        return (
          <div key={layer.kind} className="flex min-w-[168px] flex-1 flex-col">
            <div className="mb-2 flex items-center gap-2 px-1">
              <span className="text-[11px] font-medium uppercase tracking-wide text-[var(--text-subtle)]">
                {layer.label}
              </span>
              <span className="truncate text-[10.5px] text-[var(--text-subtle)]">
                {layer.hint}
              </span>
              {index < present.length - 1 && (
                <ArrowRight
                  className="ml-auto size-3 shrink-0 text-[var(--text-subtle)]"
                  aria-hidden
                />
              )}
            </div>

            <ul className="min-h-0 flex-1 space-y-1 overflow-y-auto pr-1">
              {nodes.map((node) => {
                const isSelected = node.id === selected;
                const isRelated = related.has(node.id);
                const dim = selected !== null && !isSelected && !isRelated;
                const transform = transformOf.get(node.id);

                return (
                  <li key={node.id}>
                    <button
                      type="button"
                      onClick={() => onSelect(node)}
                      className={cn(
                        "w-full rounded-[var(--radius-sm)] border px-2 py-1.5 text-left transition-opacity",
                        isSelected
                          ? "border-[var(--accent)] bg-[var(--accent-soft)]"
                          : "border-[var(--line)] bg-[var(--surface)] hover:border-[var(--border)]",
                        dim && "opacity-30",
                      )}
                    >
                      <span className="flex items-center gap-1.5">
                        {isSelected && (
                          <CircleDot
                            className="size-3 shrink-0 text-[var(--accent)]"
                            aria-hidden
                          />
                        )}
                        <span className="truncate text-[12px] text-[var(--text)]">
                          {/* A count has no column of its own - it counts rows - so the
                              graph records `*`. Rendering that raw reads as a glitch. */}
                          {node.column === "*" ? "all rows" : node.column}
                        </span>
                      </span>
                      <span className="mt-0.5 block truncate text-[10.5px] text-[var(--text-subtle)]">
                        {node.table}
                      </span>
                      {/* Shown only when something happened. A transform on every node
                          would be noise; a transform on the ones that changed is the
                          difference between "it travelled" and "it was changed here". */}
                      {transform && transform !== "landed unchanged" && (
                        <code className="mt-1 block truncate rounded-[3px] bg-[var(--surface-sunken)] px-1 py-0.5 font-mono text-[10px] text-[var(--text-muted)]">
                          {transform}
                        </code>
                      )}
                    </button>
                  </li>
                );
              })}
            </ul>
          </div>
        );
      })}
    </div>
  );
}
