"use client";

import { useQuery } from "@tanstack/react-query";
import { X } from "lucide-react";
import { api } from "@/lib/api";
import { formatCount } from "@/lib/format";
import type { Layer, SampleResult } from "@/lib/types";
import { Skeleton } from "@/components/ui/Skeleton";
import { LAYER_COLORS } from "@/components/datasets/LayerBadge";

export function TablePreview({
  layer,
  table,
  rows,
  onClose,
}: {
  layer: Layer;
  table: string;
  rows: number;
  onClose: () => void;
}) {
  const { data, isLoading, error } = useQuery({
    queryKey: ["preview", layer, table],
    queryFn: () =>
      api.get<SampleResult>(`/api/warehouse/${layer}/${table}/preview?limit=200`),
  });

  return (
    <div className="flex min-h-0 flex-1 flex-col border-t border-[var(--line)] bg-[var(--surface)]">
      <header className="flex shrink-0 items-center gap-2 border-b border-[var(--line)] px-4 py-2">
        <span
          className="size-1.5 rounded-full"
          style={{ background: LAYER_COLORS[layer].color }}
          aria-hidden
        />
        <span className="font-mono text-[12px] text-[var(--text)]">
          {layer}.{table}
        </span>
        <span className="text-[11.5px] tabular-nums text-[var(--text-subtle)]">
          {formatCount(rows)} rows
        </span>
        <button
          onClick={onClose}
          aria-label="Close preview"
          className="ml-auto rounded-[var(--radius-sm)] p-1 text-[var(--text-subtle)] hover:bg-[var(--surface-hover)] hover:text-[var(--text)]"
        >
          <X className="size-3.5" aria-hidden />
        </button>
      </header>

      <div className="min-h-0 flex-1 overflow-auto">
        {isLoading ? (
          <div className="space-y-1 p-3">
            {Array.from({ length: 6 }, (_, i) => (
              <Skeleton key={i} className="h-5" />
            ))}
          </div>
        ) : error ? (
          <p className="p-4 text-[12px] text-[var(--color-critical)]">
            {(error as Error).message}
          </p>
        ) : (
          <table className="w-max min-w-full">
            <thead className="sticky top-0 bg-[var(--surface-sunken)]">
              <tr>
                {data?.columns.map((column) => (
                  <th
                    key={column}
                    className="whitespace-nowrap border-b border-[var(--line)] px-3 py-1.5 text-left font-mono text-[10.5px] font-medium text-[var(--text-subtle)]"
                  >
                    {column}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {data?.rows.map((row, index) => (
                <tr key={index} className="hover:bg-[var(--surface-hover)]">
                  {row.map((value, cell) => (
                    <td
                      key={cell}
                      className="max-w-[280px] truncate whitespace-nowrap border-b border-[var(--line)] px-3 py-1 font-mono text-[11.5px] tabular-nums text-[var(--text-muted)]"
                    >
                      {value === null ? (
                        <span className="italic text-[var(--text-subtle)]">null</span>
                      ) : (
                        String(value)
                      )}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
