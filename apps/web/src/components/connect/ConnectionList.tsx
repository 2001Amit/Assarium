"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { CircleDashed, CircleCheck, CircleX, RefreshCw, Trash2 } from "lucide-react";
import { api } from "@/lib/api";
import { formatRelative } from "@/lib/format";
import type { Connection, ConnectionTestResult, SourceSpec } from "@/lib/types";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Skeleton } from "@/components/ui/Skeleton";
import { sourceIcon } from "./sourceIcon";

const STATUS = {
  ok: { icon: CircleCheck, tone: "positive" as const, label: "Connected" },
  failed: { icon: CircleX, tone: "critical" as const, label: "Failed" },
  untested: { icon: CircleDashed, tone: "neutral" as const, label: "Untested" },
};

function ConnectionRow({ connection, spec }: { connection: Connection; spec?: SourceSpec }) {
  const queryClient = useQueryClient();
  const Icon = sourceIcon(spec?.icon ?? "database", spec?.name);
  const status = STATUS[connection.status];

  const retest = useMutation({
    mutationFn: () =>
      api.post<ConnectionTestResult>(`/api/connections/${connection.id}/test`),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["connections"] }),
  });

  const remove = useMutation({
    mutationFn: () => api.del<void>(`/api/connections/${connection.id}`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["connections"] }),
  });

  const target = String(
    connection.config.host ??
      connection.config.account ??
      connection.config.account_name ??
      connection.config.project_id ??
      connection.config.instance_url ??
      connection.config.site_url ??
      connection.config.label ??
      "",
  );

  return (
    <li className="group flex items-center gap-3 border-b border-[var(--line)] px-4 py-2.5 last:border-b-0 hover:bg-[var(--surface-hover)]">
      <span className="flex size-7 shrink-0 items-center justify-center rounded-[var(--radius-md)] border border-[var(--line)] bg-[var(--surface-sunken)]">
        <Icon className="size-4" aria-hidden />
      </span>

      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <Link
            href={`/datasets?connection=${connection.id}`}
            className="truncate text-[13px] font-medium text-[var(--text)] hover:text-[var(--accent)]"
          >
            {connection.name}
          </Link>
          <Badge tone={status.tone}>{status.label}</Badge>
        </div>
        <p className="truncate text-[11.5px] text-[var(--text-subtle)]">
          {connection.source_name}
          {target && <span className="font-mono"> · {target}</span>}
          <span> · tested {formatRelative(connection.last_tested_at)}</span>
        </p>
      </div>

      <span className="hidden shrink-0 text-[11.5px] tabular-nums text-[var(--text-subtle)] sm:block">
        {connection.dataset_count} {connection.dataset_count === 1 ? "dataset" : "datasets"}
      </span>

      <div className="flex shrink-0 items-center gap-1 opacity-0 transition-opacity group-hover:opacity-100 focus-within:opacity-100">
        <Button
          size="sm"
          variant="ghost"
          icon={RefreshCw}
          loading={retest.isPending}
          onClick={() => retest.mutate()}
          aria-label={`Re-test ${connection.name}`}
        />
        <Button
          size="sm"
          variant="ghost"
          icon={Trash2}
          loading={remove.isPending}
          onClick={() => remove.mutate()}
          aria-label={`Remove ${connection.name}`}
        />
      </div>
    </li>
  );
}

export function ConnectionList({ specs }: { specs: SourceSpec[] }) {
  const { data, isLoading } = useQuery({
    queryKey: ["connections"],
    queryFn: () => api.get<Connection[]>("/api/connections"),
  });

  if (isLoading) {
    return (
      <div className="space-y-2 p-4">
        {[0, 1].map((i) => (
          <Skeleton key={i} className="h-11 w-full" />
        ))}
      </div>
    );
  }

  if (!data?.length) return null;

  const byId = new Map(specs.map((s) => [s.source_id, s]));
  return (
    <ul>
      {data.map((connection) => (
        <ConnectionRow
          key={connection.id}
          connection={connection}
          spec={byId.get(connection.source_id)}
        />
      ))}
    </ul>
  );
}
