"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Table2, Trash2 } from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { formatCount, formatRelative } from "@/lib/format";
import type { Dataset } from "@/lib/types";
import { Button } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/EmptyState";
import { LayerBadge } from "./LayerBadge";

export function DatasetTable({
  datasets,
  activeId,
  onSelect,
  onProfileAll,
  profilingId,
}: {
  datasets: Dataset[];
  activeId: string | null;
  onSelect: (id: string) => void;
  onProfileAll: () => void;
  profilingId: string | null;
}) {
  const queryClient = useQueryClient();

  const remove = useMutation({
    mutationFn: (id: string) => api.del<void>(`/api/datasets/${id}`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["datasets"] });
      queryClient.invalidateQueries({ queryKey: ["connections"] });
    },
  });

  const unprofiled = datasets.filter((d) => !d.profiled_at).length;

  if (datasets.length === 0) {
    return (
      <EmptyState
        icon={Table2}
        title="Nothing selected yet"
        description="Browse the source above and tick the tables, files or objects you want to work with."
      />
    );
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex shrink-0 items-center justify-between gap-3 border-b border-[var(--line)] px-4 py-2">
        <p className="text-[12px] text-[var(--text-muted)]">
          <span className="font-medium text-[var(--text)] tabular-nums">{datasets.length}</span>{" "}
          selected
          {unprofiled > 0 && (
            <span className="text-[var(--text-subtle)]"> · {unprofiled} not yet profiled</span>
          )}
        </p>
        {unprofiled > 0 && (
          <Button size="sm" onClick={onProfileAll} loading={Boolean(profilingId)}>
            Profile all
          </Button>
        )}
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        <table className="w-full">
          <thead className="sticky top-0 z-10 bg-[var(--surface)]">
            <tr className="border-b border-[var(--line)] text-[10.5px] uppercase tracking-[0.06em] text-[var(--text-subtle)]">
              <th className="px-4 py-1.5 text-left font-semibold">Dataset</th>
              <th className="px-3 py-1.5 text-left font-semibold">Layer</th>
              <th className="px-3 py-1.5 text-right font-semibold">Rows</th>
              <th className="px-3 py-1.5 text-right font-semibold">Columns</th>
              <th className="px-3 py-1.5 text-left font-semibold">Profiled</th>
              <th className="w-8" />
            </tr>
          </thead>
          <tbody>
            {datasets.map((dataset) => {
              const active = dataset.id === activeId;
              return (
                <tr
                  key={dataset.id}
                  onClick={() => onSelect(dataset.id)}
                  className={cn(
                    "group cursor-pointer border-b border-[var(--line)] last:border-b-0",
                    active ? "bg-[var(--accent-soft)]" : "hover:bg-[var(--surface-hover)]",
                  )}
                >
                  <td className="max-w-0 px-4 py-2">
                    <p className="truncate text-[12.5px] font-medium text-[var(--text)]">
                      {dataset.name}
                    </p>
                    <p className="truncate font-mono text-[10.5px] text-[var(--text-subtle)]">
                      {dataset.path.slice(0, -1).join(" / ") || "—"}
                    </p>
                  </td>
                  <td className="px-3 py-2">
                    {profilingId === dataset.id ? (
                      <span className="text-[11.5px] text-[var(--text-subtle)]">profiling…</span>
                    ) : (
                      <LayerBadge
                        layer={dataset.effective_layer}
                        confidence={dataset.layer_confidence}
                        overridden={Boolean(dataset.layer_override)}
                      />
                    )}
                  </td>
                  <td className="px-3 py-2 text-right text-[12px] tabular-nums text-[var(--text-muted)]">
                    {formatCount(dataset.row_estimate)}
                  </td>
                  <td className="px-3 py-2 text-right text-[12px] tabular-nums text-[var(--text-muted)]">
                    {dataset.column_count}
                  </td>
                  <td className="px-3 py-2 text-[11.5px] text-[var(--text-subtle)]">
                    {dataset.profiled_at ? formatRelative(dataset.profiled_at) : "never"}
                  </td>
                  <td className="px-2 py-2">
                    <Button
                      size="sm"
                      variant="ghost"
                      icon={Trash2}
                      aria-label={`Remove ${dataset.name}`}
                      className="opacity-0 group-hover:opacity-100 focus:opacity-100"
                      onClick={(event) => {
                        event.stopPropagation();
                        remove.mutate(dataset.id);
                      }}
                    />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
