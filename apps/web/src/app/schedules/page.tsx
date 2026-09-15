"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Clock, Plus, X } from "lucide-react";
import { api, AssariumApiError } from "@/lib/api";
import type { Connection, Schedule, ScheduleOverview } from "@/lib/types";
import { PageHeader } from "@/components/PageHeader";
import { ScheduleCard } from "@/components/schedules/ScheduleCard";
import { CronInput } from "@/components/schedules/CronInput";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Callout } from "@/components/ui/Callout";
import { EmptyState } from "@/components/ui/EmptyState";
import { cn } from "@/lib/cn";
import { inputClass } from "@/components/ui/Field";

export default function SchedulesPage() {
  const queryClient = useQueryClient();
  const [creating, setCreating] = useState(false);
  const [selectedConnectionId, setSelectedConnectionId] = useState<string | null>(null);
  const [cron, setCron] = useState("0 2 * * *");
  const [error, setError] = useState<string | null>(null);
  const [detailSchedule, setDetailSchedule] = useState<Schedule | null>(null);

  const { data: overview } = useQuery({
    queryKey: ["schedules"],
    queryFn: () => api.get<ScheduleOverview>("/api/schedules"),
    refetchInterval: 15_000,
  });

  const { data: connections } = useQuery({
    queryKey: ["connections"],
    queryFn: () => api.get<Connection[]>("/api/connections"),
  });

  // Connections that don't have a schedule yet
  const unscheduled = connections?.filter(
    (c) => !overview?.schedules.some((s) => s.connection_id === c.id),
  );

  const createSchedule = useMutation({
    mutationFn: (connectionId: string) =>
      api.post<Schedule>(`/api/connections/${connectionId}/schedule`, {
        cron,
        enabled: true,
      }),
    onMutate: () => setError(null),
    onSuccess: () => {
      setCreating(false);
      setSelectedConnectionId(null);
      setCron("0 2 * * *");
      queryClient.invalidateQueries({ queryKey: ["schedules"] });
    },
    onError: (err) =>
      setError(err instanceof AssariumApiError ? err.message : "Could not create the schedule."),
  });

  const toggleSchedule = useMutation({
    mutationFn: ({ connectionId, enabled }: { connectionId: string; enabled: boolean }) =>
      api.patch<Schedule>(`/api/connections/${connectionId}/schedule`, { enabled }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["schedules"] }),
  });

  const deleteSchedule = useMutation({
    mutationFn: (connectionId: string) =>
      api.del(`/api/connections/${connectionId}/schedule`),
    onSuccess: () => {
      setDetailSchedule(null);
      queryClient.invalidateQueries({ queryKey: ["schedules"] });
    },
  });

  return (
    <>
      <PageHeader
        title="Schedules"
        description="Automated pipeline runs. Each connection can have one schedule."
        actions={
          unscheduled && unscheduled.length > 0 ? (
            <Button size="sm" variant="primary" icon={Plus} onClick={() => setCreating(true)}>
              Add schedule
            </Button>
          ) : undefined
        }
      />

      <div className="flex min-h-0 flex-1 overflow-hidden">
        {/* Main list */}
        <div className="flex min-w-0 flex-1 flex-col overflow-y-auto">
          {/* Stats row */}
          {overview && overview.total > 0 && (
            <div className="flex items-center gap-3 border-b border-[var(--line)] px-4 py-2.5">
              <div className="flex items-center gap-1.5 text-[12px] text-[var(--text-muted)]">
                <span className="font-medium text-[var(--text)]">{overview.total}</span> total
              </div>
              <span className="text-[var(--text-subtle)]">·</span>
              <Badge tone="positive">{overview.active} active</Badge>
              {overview.paused > 0 && <Badge tone="neutral">{overview.paused} paused</Badge>}
              {overview.failing > 0 && <Badge tone="critical">{overview.failing} failing</Badge>}
            </div>
          )}

          {/* Create form */}
          {creating && (
            <div className="border-b border-[var(--line)] bg-[var(--surface-sunken)] px-4 py-4">
              <div className="flex items-center justify-between">
                <p className="text-[13px] font-medium text-[var(--text)]">New schedule</p>
                <button
                  onClick={() => {
                    setCreating(false);
                    setError(null);
                  }}
                  className="flex size-6 items-center justify-center rounded-[var(--radius-sm)] text-[var(--text-muted)] hover:bg-[var(--surface-hover)]"
                >
                  <X className="size-3.5" aria-hidden />
                </button>
              </div>
              <div className="mt-3 flex flex-col gap-3">
                {/* Connection selector */}
                <div className="flex flex-col gap-1.5">
                  <label className="text-[12px] font-medium text-[var(--text-muted)]">
                    Connection
                  </label>
                  <select
                    value={selectedConnectionId ?? ""}
                    onChange={(e) => setSelectedConnectionId(e.target.value || null)}
                    className={cn(inputClass, "text-[12.5px]")}
                  >
                    <option value="">Select a connection</option>
                    {unscheduled?.map((c) => (
                      <option key={c.id} value={c.id}>
                        {c.name}
                      </option>
                    ))}
                  </select>
                </div>

                {/* Cron input */}
                <div className="flex flex-col gap-1.5">
                  <label className="text-[12px] font-medium text-[var(--text-muted)]">
                    Schedule
                  </label>
                  <CronInput value={cron} onChange={setCron} error={error} />
                </div>

                <Button
                  variant="primary"
                  size="sm"
                  loading={createSchedule.isPending}
                  disabled={!selectedConnectionId || !cron}
                  onClick={() => selectedConnectionId && createSchedule.mutate(selectedConnectionId)}
                >
                  Create schedule
                </Button>
              </div>
            </div>
          )}

          {/* Error */}
          {error && !creating && (
            <div className="px-4 pt-3">
              <Callout tone="critical">{error}</Callout>
            </div>
          )}

          {/* Schedule list */}
          {overview && overview.total > 0 ? (
            <div className="flex flex-col gap-2 p-4">
              {overview.schedules.map((schedule) => (
                <ScheduleCard
                  key={schedule.id}
                  schedule={schedule}
                  onToggle={(enabled) =>
                    toggleSchedule.mutate({
                      connectionId: schedule.connection_id,
                      enabled,
                    })
                  }
                  onDelete={() => deleteSchedule.mutate(schedule.connection_id)}
                  onClick={() => setDetailSchedule(schedule)}
                />
              ))}
            </div>
          ) : (
            !creating && (
              <EmptyState
                icon={Clock}
                title="No schedules yet"
                description="Add a schedule to automate pipeline runs for a connection."
                action={
                  connections && connections.length > 0 ? (
                    <Button size="sm" variant="primary" icon={Plus} onClick={() => setCreating(true)}>
                      Add schedule
                    </Button>
                  ) : undefined
                }
              />
            )
          )}
        </div>

        {/* Detail panel */}
        {detailSchedule && (
          <aside className="flex w-[340px] shrink-0 flex-col border-l border-[var(--line)] bg-[var(--surface)]">
            <div className="flex items-center justify-between border-b border-[var(--line)] px-4 py-3">
              <p className="text-[13px] font-medium text-[var(--text)]">
                {detailSchedule.connection_name}
              </p>
              <button
                onClick={() => setDetailSchedule(null)}
                className="flex size-6 items-center justify-center rounded-[var(--radius-sm)] text-[var(--text-muted)] hover:bg-[var(--surface-hover)]"
              >
                <X className="size-3.5" aria-hidden />
              </button>
            </div>
            <div className="flex-1 overflow-y-auto px-4 py-3">
              <dl className="flex flex-col gap-3 text-[12.5px]">
                <div>
                  <dt className="text-[11px] font-medium uppercase tracking-[0.06em] text-[var(--text-subtle)]">
                    Cron expression
                  </dt>
                  <dd className="mt-0.5 font-mono text-[var(--text)]">{detailSchedule.cron}</dd>
                </div>
                <div>
                  <dt className="text-[11px] font-medium uppercase tracking-[0.06em] text-[var(--text-subtle)]">
                    Status
                  </dt>
                  <dd className="mt-0.5 text-[var(--text)]">
                    {detailSchedule.enabled ? "Enabled" : "Paused"}
                    {detailSchedule.locked_by && ` · Running on ${detailSchedule.locked_by}`}
                  </dd>
                </div>
                <div>
                  <dt className="text-[11px] font-medium uppercase tracking-[0.06em] text-[var(--text-subtle)]">
                    Next run
                  </dt>
                  <dd className="mt-0.5 text-[var(--text)]">
                    {detailSchedule.next_run_at
                      ? new Date(detailSchedule.next_run_at).toLocaleString()
                      : "—"}
                  </dd>
                </div>
                <div>
                  <dt className="text-[11px] font-medium uppercase tracking-[0.06em] text-[var(--text-subtle)]">
                    Last run
                  </dt>
                  <dd className="mt-0.5 text-[var(--text)]">
                    {detailSchedule.last_run_at
                      ? new Date(detailSchedule.last_run_at).toLocaleString()
                      : "Never"}
                    {detailSchedule.last_status && (
                      <Badge
                        tone={detailSchedule.last_status === "succeeded" ? "positive" : "critical"}
                        className="ml-2"
                      >
                        {detailSchedule.last_status}
                      </Badge>
                    )}
                  </dd>
                </div>
                <div>
                  <dt className="text-[11px] font-medium uppercase tracking-[0.06em] text-[var(--text-subtle)]">
                    Consecutive failures
                  </dt>
                  <dd className="mt-0.5 text-[var(--text)]">
                    {detailSchedule.consecutive_failures} / {detailSchedule.max_retries} max
                  </dd>
                </div>
                {detailSchedule.last_error && (
                  <div>
                    <dt className="text-[11px] font-medium uppercase tracking-[0.06em] text-[var(--text-subtle)]">
                      Last error
                    </dt>
                    <dd className="mt-0.5 whitespace-pre-wrap text-[12px] text-[var(--color-critical)]">
                      {detailSchedule.last_error}
                    </dd>
                  </div>
                )}
                <div>
                  <dt className="text-[11px] font-medium uppercase tracking-[0.06em] text-[var(--text-subtle)]">
                    Created
                  </dt>
                  <dd className="mt-0.5 text-[var(--text)]">
                    {new Date(detailSchedule.created_at).toLocaleString()}
                  </dd>
                </div>
              </dl>
            </div>
          </aside>
        )}
      </div>
    </>
  );
}
