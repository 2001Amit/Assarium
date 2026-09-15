"use client";

import { Suspense, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, Layers, Play } from "lucide-react";
import { api, AssariumApiError } from "@/lib/api";
import { cn } from "@/lib/cn";
import type { Connection, Layer, PipelineRun, Warehouse } from "@/lib/types";
import { PageHeader } from "@/components/PageHeader";
import { MedallionView } from "@/components/pipeline/MedallionView";
import { RunTimeline } from "@/components/pipeline/RunTimeline";
import { TablePreview } from "@/components/pipeline/TablePreview";
import { Button } from "@/components/ui/Button";
import { Callout } from "@/components/ui/Callout";
import { EmptyState } from "@/components/ui/EmptyState";
import { inputClass } from "@/components/ui/Field";

function PipelineWorkspace() {
  const params = useSearchParams();
  const queryClient = useQueryClient();

  const [connectionId, setConnectionId] = useState<string | null>(params.get("connection"));
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [preview, setPreview] = useState<{ layer: Layer; table: string; rows: number } | null>(
    null,
  );
  const [error, setError] = useState<string | null>(null);

  const { data: connections } = useQuery({
    queryKey: ["connections"],
    queryFn: () => api.get<Connection[]>("/api/connections"),
  });

  useEffect(() => {
    if (!connectionId && connections?.length) setConnectionId(connections[0].id);
  }, [connections, connectionId]);

  const { data: warehouse } = useQuery({
    queryKey: ["warehouse"],
    queryFn: () => api.get<Warehouse>("/api/warehouse"),
  });

  const { data: runs } = useQuery({
    queryKey: ["runs", connectionId],
    queryFn: () => api.get<PipelineRun[]>(`/api/connections/${connectionId}/runs`),
    enabled: Boolean(connectionId),
  });

  const currentRunId = activeRunId ?? runs?.[0]?.id ?? null;

  const { data: run } = useQuery({
    queryKey: ["run", currentRunId],
    queryFn: () => api.get<PipelineRun>(`/api/runs/${currentRunId}`),
    enabled: Boolean(currentRunId),
    // While a run is in flight, poll so steps appear as they complete.
    refetchInterval: (query) =>
      (query.state.data as PipelineRun | undefined)?.status === "running" ? 1200 : false,
  });

  // Refresh the warehouse view once a run stops moving.
  useEffect(() => {
    if (run && run.status !== "running") {
      queryClient.invalidateQueries({ queryKey: ["warehouse"] });
    }
  }, [run?.status, run, queryClient]);

  const startRun = useMutation({
    mutationFn: () => api.post<PipelineRun>(`/api/connections/${connectionId}/runs`, {}),
    onMutate: () => setError(null),
    onSuccess: (created) => {
      setActiveRunId(created.id);
      queryClient.invalidateQueries({ queryKey: ["runs", connectionId] });
    },
    onError: (failure) =>
      setError(
        failure instanceof AssariumApiError ? failure.message : "The run could not be started.",
      ),
  });

  if (connections && connections.length === 0) {
    return (
      <>
        <PageHeader title="Pipeline" />
        <EmptyState
          icon={Layers}
          title="Nothing to refine yet"
          description="Connect a source and select some datasets first."
        />
      </>
    );
  }

  return (
    <>
      <PageHeader
        title="Pipeline"
        description="Land, refine and summarise. Every step records what it did and the SQL it ran."
        actions={
          <>
            <div className="relative">
              <select
                aria-label="Connection"
                value={connectionId ?? ""}
                onChange={(event) => {
                  setConnectionId(event.target.value);
                  setActiveRunId(null);
                }}
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
              variant="primary"
              icon={Play}
              loading={startRun.isPending || run?.status === "running"}
              onClick={() => startRun.mutate()}
            >
              Run refinement
            </Button>
          </>
        }
      />

      <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
        <div className={cn("min-h-0 overflow-y-auto", preview ? "flex-none" : "flex-1")}>
          {error && (
            <div className="px-4 pt-4">
              <Callout tone="critical" title="Cannot start the run">
                {error}
              </Callout>
            </div>
          )}

          {warehouse && (
            <MedallionView
              warehouse={warehouse}
              selected={preview}
              onSelect={(layer, table, rows) => setPreview({ layer, table, rows })}
            />
          )}

          {run && <RunTimeline run={run} />}

          {runs && runs.length > 1 && (
            <div className="px-4 pb-4">
              <p className="mb-1.5 text-[11px] font-semibold uppercase tracking-[0.06em] text-[var(--text-subtle)]">
                Earlier runs
              </p>
              <div className="flex flex-wrap gap-1.5">
                {runs.slice(0, 12).map((earlier) => (
                  <button
                    key={earlier.id}
                    onClick={() => setActiveRunId(earlier.id)}
                    className={cn(
                      "rounded-[var(--radius-sm)] border px-2 py-1 font-mono text-[10.5px]",
                      earlier.id === currentRunId
                        ? "border-[var(--accent)] text-[var(--accent)]"
                        : "border-[var(--line)] text-[var(--text-muted)] hover:bg-[var(--surface-hover)]",
                    )}
                  >
                    {new Date(earlier.started_at).toLocaleTimeString()} · {earlier.status}
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>

        {preview && (
          <TablePreview
            key={`${preview.layer}.${preview.table}`}
            layer={preview.layer}
            table={preview.table}
            rows={preview.rows}
            onClose={() => setPreview(null)}
          />
        )}
      </div>
    </>
  );
}

export default function PipelinePage() {
  return (
    <Suspense fallback={<PageHeader title="Pipeline" />}>
      <PipelineWorkspace />
    </Suspense>
  );
}
