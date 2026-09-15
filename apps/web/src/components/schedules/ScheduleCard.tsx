"use client";

import {
  AlertTriangle,
  CheckCircle2,
  Clock,
  Loader2,
  Pause,
  Play,
  Trash2,
  XCircle,
} from "lucide-react";
import { cn } from "@/lib/cn";
import type { Schedule } from "@/lib/types";
import { Badge } from "@/components/ui/Badge";

function statusConfig(schedule: Schedule) {
  if (!schedule.enabled) {
    return { tone: "neutral" as const, label: "Paused", Icon: Pause };
  }
  if (schedule.locked_at) {
    return { tone: "info" as const, label: "Running", Icon: Loader2 };
  }
  if (schedule.consecutive_failures > 0) {
    return {
      tone: "critical" as const,
      label: `Failing (${schedule.consecutive_failures})`,
      Icon: AlertTriangle,
    };
  }
  if (schedule.last_status === "succeeded") {
    return { tone: "positive" as const, label: "Active", Icon: CheckCircle2 };
  }
  return { tone: "accent" as const, label: "Scheduled", Icon: Clock };
}

function formatTime(iso: string | null): string {
  if (!iso) return "—";
  const date = new Date(iso);
  return date.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function formatCronShort(cron: string): string {
  if (cron === "0 * * * *") return "Hourly";
  if (cron === "0 2 * * *") return "Daily 02:00";
  if (cron === "0 6 * * *") return "Daily 06:00";
  if (cron === "0 */6 * * *") return "Every 6h";
  if (cron === "0 2 * * 1") return "Weekly Mon";
  return cron;
}

export function ScheduleCard({
  schedule,
  onToggle,
  onDelete,
  onClick,
}: {
  schedule: Schedule;
  onToggle: (enabled: boolean) => void;
  onDelete: () => void;
  onClick: () => void;
}) {
  const { tone, label, Icon } = statusConfig(schedule);

  return (
    <div
      className={cn(
        "group flex items-center gap-3 rounded-[var(--radius-md)] border border-[var(--line)] bg-[var(--surface)] px-3.5 py-3 transition-colors",
        "hover:border-[var(--line-strong)] hover:bg-[var(--surface-hover)]",
        "cursor-pointer",
      )}
      onClick={onClick}
    >
      {/* Status icon */}
      <div
        className={cn(
          "flex size-8 shrink-0 items-center justify-center rounded-[var(--radius-sm)]",
          schedule.locked_at
            ? "bg-[var(--color-info)]/10"
            : schedule.consecutive_failures > 0
              ? "bg-[var(--color-critical)]/10"
              : schedule.enabled
                ? "bg-[var(--color-positive)]/10"
                : "bg-[var(--surface-sunken)]",
        )}
      >
        <Icon
          className={cn(
            "size-3.5",
            schedule.locked_at && "animate-spin text-[var(--color-info)]",
            schedule.consecutive_failures > 0 && "text-[var(--color-critical)]",
            schedule.enabled &&
              !schedule.locked_at &&
              schedule.consecutive_failures === 0 &&
              "text-[var(--color-positive)]",
            !schedule.enabled && "text-[var(--text-subtle)]",
          )}
          aria-hidden
        />
      </div>

      {/* Details */}
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <p className="truncate text-[13px] font-medium text-[var(--text)]">
            {schedule.connection_name}
          </p>
          <Badge tone={tone}>{label}</Badge>
        </div>
        <div className="mt-0.5 flex items-center gap-3 text-[11.5px] text-[var(--text-muted)]">
          <span className="font-mono">{formatCronShort(schedule.cron)}</span>
          <span className="text-[var(--text-subtle)]">·</span>
          <span>Next: {formatTime(schedule.next_run_at)}</span>
          {schedule.last_run_at && (
            <>
              <span className="text-[var(--text-subtle)]">·</span>
              <span>Last: {formatTime(schedule.last_run_at)}</span>
            </>
          )}
        </div>
        {schedule.last_error && (
          <p className="mt-1 truncate text-[11.5px] text-[var(--color-critical)]">
            {schedule.last_error}
          </p>
        )}
      </div>

      {/* Actions */}
      <div className="flex shrink-0 items-center gap-1 opacity-0 transition-opacity group-hover:opacity-100">
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            onToggle(!schedule.enabled);
          }}
          title={schedule.enabled ? "Pause schedule" : "Resume schedule"}
          className="flex size-7 items-center justify-center rounded-[var(--radius-sm)] text-[var(--text-muted)] transition-colors hover:bg-[var(--surface-sunken)] hover:text-[var(--text)]"
        >
          {schedule.enabled ? (
            <Pause className="size-3.5" aria-hidden />
          ) : (
            <Play className="size-3.5" aria-hidden />
          )}
        </button>
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            onDelete();
          }}
          title="Delete schedule"
          className="flex size-7 items-center justify-center rounded-[var(--radius-sm)] text-[var(--text-muted)] transition-colors hover:bg-[var(--color-critical)]/10 hover:text-[var(--color-critical)]"
        >
          <Trash2 className="size-3.5" aria-hidden />
        </button>
      </div>
    </div>
  );
}
