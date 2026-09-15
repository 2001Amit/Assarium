"use client";

import { Suspense, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, RefreshCw, Share2 } from "lucide-react";
import { api, AssariumApiError } from "@/lib/api";
import { cn } from "@/lib/cn";
import type { Connection, OntologyGraph } from "@/lib/types";
import { PageHeader } from "@/components/PageHeader";
import { NodeDetail, OntologyCanvas } from "@/components/ontology/OntologyCanvas";
import { Button } from "@/components/ui/Button";
import { Callout } from "@/components/ui/Callout";
import { EmptyState } from "@/components/ui/EmptyState";
import { inputClass } from "@/components/ui/Field";

function OntologyWorkspace() {
  const params = useSearchParams();
  const queryClient = useQueryClient();
  const [connectionId, setConnectionId] = useState<string | null>(params.get("connection"));
  const [selected, setSelected] = useState<string | null>(null);

  const { data: connections } = useQuery({
    queryKey: ["connections"],
    queryFn: () => api.get<Connection[]>("/api/connections"),
  });

  useEffect(() => {
    if (!connectionId && connections?.length) setConnectionId(connections[0].id);
  }, [connections, connectionId]);

  const { data: graph, error } = useQuery({
    queryKey: ["ontology", connectionId],
    queryFn: () => api.get<OntologyGraph>(`/api/connections/${connectionId}/ontology`),
    enabled: Boolean(connectionId),
    retry: false,
  });

  const rebuild = useMutation({
    mutationFn: () =>
      api.post(`/api/connections/${connectionId}/semantic/build`, { overwrite_edits: true }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["ontology", connectionId] });
      queryClient.invalidateQueries({ queryKey: ["semantic", connectionId] });
    },
  });

  const notFound = error instanceof AssariumApiError && error.status === 404;
  const node = graph?.nodes.find((n) => n.id === selected) ?? null;

  return (
    <>
      <PageHeader
        title="Ontology"
        description="How your entities relate, and what each one can answer."
        actions={
          <>
            <div className="relative">
              <select
                aria-label="Connection"
                value={connectionId ?? ""}
                onChange={(event) => {
                  setConnectionId(event.target.value);
                  setSelected(null);
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
              icon={RefreshCw}
              loading={rebuild.isPending}
              onClick={() => rebuild.mutate()}
            >
              Rebuild model
            </Button>
          </>
        }
      />

      {notFound ? (
        <EmptyState
          icon={Share2}
          title="No model built yet"
          description="Build the semantic model to see how your entities connect."
          action={
            <Button variant="primary" loading={rebuild.isPending} onClick={() => rebuild.mutate()}>
              Build the model
            </Button>
          }
        />
      ) : (
        <div className="flex min-h-0 flex-1">
          <div className="flex min-w-0 flex-1 flex-col">
            {graph && graph.notes.length > 0 && (
              <div className="shrink-0 space-y-1.5 px-4 pt-3">
                {graph.notes.map((note) => (
                  <Callout key={note} tone="caution">
                    {note}
                  </Callout>
                ))}
              </div>
            )}
            <div className="min-h-0 flex-1">
              {graph && (
                <OntologyCanvas graph={graph} selected={selected} onSelect={setSelected} />
              )}
            </div>
          </div>

          {node && graph && (
            <NodeDetail
              node={node}
              edges={graph.edges}
              nodes={graph.nodes}
              onClose={() => setSelected(null)}
            />
          )}
        </div>
      )}
    </>
  );
}

export default function OntologyPage() {
  return (
    <Suspense fallback={<PageHeader title="Ontology" />}>
      <OntologyWorkspace />
    </Suspense>
  );
}
