"use client";

import { Suspense, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, Plug, Share2 } from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import type { Connection, Dataset, Relationship, SourceSpec } from "@/lib/types";
import { PageHeader } from "@/components/PageHeader";
import { DatasetDetailPanel } from "@/components/datasets/DatasetDetail";
import { DatasetTable } from "@/components/datasets/DatasetTable";
import { SourceBrowser } from "@/components/datasets/SourceBrowser";
import { Button } from "@/components/ui/Button";
import { Callout } from "@/components/ui/Callout";
import { EmptyState } from "@/components/ui/EmptyState";
import { inputClass } from "@/components/ui/Field";

function DatasetsWorkspace() {
  const params = useSearchParams();
  const queryClient = useQueryClient();

  const [connectionId, setConnectionId] = useState<string | null>(params.get("connection"));
  const [activeDataset, setActiveDataset] = useState<string | null>(null);
  const [browserOpen, setBrowserOpen] = useState(true);
  const [profilingId, setProfilingId] = useState<string | null>(null);
  const [profileError, setProfileError] = useState<string | null>(null);

  const { data: connections } = useQuery({
    queryKey: ["connections"],
    queryFn: () => api.get<Connection[]>("/api/connections"),
  });

  const { data: specs } = useQuery({
    queryKey: ["sources"],
    queryFn: () => api.get<SourceSpec[]>("/api/sources"),
    staleTime: Infinity,
  });

  // Fall back to the first connection so the page is never empty for no reason.
  useEffect(() => {
    if (!connectionId && connections?.length) setConnectionId(connections[0].id);
  }, [connections, connectionId]);

  const connection = connections?.find((c) => c.id === connectionId) ?? null;
  const spec = specs?.find((s) => s.source_id === connection?.source_id);

  const { data: datasets } = useQuery({
    queryKey: ["datasets", connectionId],
    queryFn: () => api.get<Dataset[]>(`/api/datasets?connection_id=${connectionId}`),
    enabled: Boolean(connectionId),
  });

  const { data: relationships } = useQuery({
    queryKey: ["relationships", connectionId],
    queryFn: () => api.get<Relationship[]>(`/api/connections/${connectionId}/relationships`),
    enabled: Boolean(connectionId),
  });

  const detectRelationships = useMutation({
    mutationFn: () => api.post<{ found: number }>(`/api/connections/${connectionId}/relationships`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["relationships", connectionId] }),
  });

  const profiled = useMemo(() => (datasets ?? []).filter((d) => d.profiled_at).length, [datasets]);

  /**
   * Profile sequentially rather than in parallel: each run issues a scan against the
   * source, and firing twenty at once is how you get throttled or noticed by a DBA.
   */
  const profileAll = async () => {
    setProfileError(null);
    for (const dataset of datasets ?? []) {
      if (dataset.profiled_at) continue;
      setProfilingId(dataset.id);
      try {
        await api.post(`/api/datasets/${dataset.id}/profile`);
      } catch (error) {
        setProfileError(`${dataset.name}: ${(error as Error).message}`);
        break;
      } finally {
        queryClient.invalidateQueries({ queryKey: ["datasets", connectionId] });
      }
    }
    setProfilingId(null);
  };

  if (connections && connections.length === 0) {
    return (
      <>
        <PageHeader title="Datasets" />
        <EmptyState
          icon={Plug}
          title="No connections yet"
          description="Connect a source first, then come back to choose what to work with."
          action={
            <Button variant="primary" onClick={() => (window.location.href = "/sources")}>
              Go to sources
            </Button>
          }
        />
      </>
    );
  }

  return (
    <>
      <PageHeader
        title="Datasets"
        description="Choose what to work with, then let Assarium profile it and place it on the medallion."
        actions={
          <>
            <div className="relative">
              <select
                aria-label="Connection"
                value={connectionId ?? ""}
                onChange={(event) => {
                  setConnectionId(event.target.value);
                  setActiveDataset(null);
                }}
                className={cn(inputClass, "h-7 appearance-none py-0 pr-7 text-[12.5px]")}
              >
                {connections?.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name}
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
              icon={Share2}
              loading={detectRelationships.isPending}
              disabled={profiled < 2}
              title={
                profiled < 2
                  ? "Profile at least two datasets first"
                  : "Find join paths between the selected datasets"
              }
              onClick={() => detectRelationships.mutate()}
            >
              Detect relationships
              {relationships && relationships.length > 0 && (
                <span className="tabular-nums text-[var(--text-subtle)]">
                  {relationships.length}
                </span>
              )}
            </Button>
          </>
        }
      />

      <div className="flex min-h-0 flex-1">
        <div className="flex min-w-0 flex-1 flex-col">
          <div className="flex shrink-0 items-center gap-2 border-b border-[var(--line)] bg-[var(--surface)] px-4 py-1.5">
            <button
              onClick={() => setBrowserOpen((open) => !open)}
              className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-[0.06em] text-[var(--text-subtle)] hover:text-[var(--text)]"
            >
              <ChevronDown
                className={cn("size-3 transition-transform", !browserOpen && "-rotate-90")}
                aria-hidden
              />
              Browse {connection?.source_name ?? "source"}
            </button>
          </div>

          {browserOpen && connection && (
            <div className="flex h-[290px] shrink-0 border-b border-[var(--line)] bg-[var(--surface)]">
              <SourceBrowser
                key={connection.id}
                connection={connection}
                spec={spec}
                selected={datasets ?? []}
              />
            </div>
          )}

          {profileError && (
            <div className="shrink-0 px-4 py-2">
              <Callout tone="critical" title="Profiling stopped">
                {profileError}
              </Callout>
            </div>
          )}

          <DatasetTable
            datasets={datasets ?? []}
            activeId={activeDataset}
            profilingId={profilingId}
            onSelect={setActiveDataset}
            onProfileAll={profileAll}
          />

          {relationships && relationships.length > 0 && (
            <div className="shrink-0 border-t border-[var(--line)] bg-[var(--surface)] px-4 py-2.5">
              <p className="mb-1.5 text-[11px] font-semibold uppercase tracking-[0.06em] text-[var(--text-subtle)]">
                Join paths found
              </p>
              <ul className="flex flex-wrap gap-1.5">
                {relationships.map((relationship) => (
                  <li
                    key={relationship.id}
                    className="flex items-center gap-1.5 rounded-[var(--radius-sm)] border border-[var(--line)] px-2 py-1 font-mono text-[11px]"
                    title={
                      relationship.kind === "declared"
                        ? "Declared by the source"
                        : `Inferred: ${Math.round((relationship.overlap ?? 0) * 100)}% of values matched`
                    }
                  >
                    <span className="text-[var(--text-muted)]">
                      {relationship.from_dataset}.{relationship.from_column}
                    </span>
                    <span className="text-[var(--text-subtle)]">&rarr;</span>
                    <span className="text-[var(--text-muted)]">
                      {relationship.to_dataset}.{relationship.to_column}
                    </span>
                    <span
                      className={cn(
                        "ml-0.5 font-sans text-[10px]",
                        relationship.kind === "declared"
                          ? "text-[var(--color-positive)]"
                          : "text-[var(--text-subtle)]",
                      )}
                    >
                      {relationship.kind === "declared"
                        ? "declared"
                        : `${Math.round(relationship.confidence * 100)}%`}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>

        {activeDataset && (
          <DatasetDetailPanel
            key={activeDataset}
            datasetId={activeDataset}
            onClose={() => setActiveDataset(null)}
          />
        )}
      </div>
    </>
  );
}

export default function DatasetsPage() {
  return (
    <Suspense fallback={<PageHeader title="Datasets" />}>
      <DatasetsWorkspace />
    </Suspense>
  );
}
