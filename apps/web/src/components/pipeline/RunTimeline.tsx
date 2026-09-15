"use client";

import { useState } from "react";
import {
  Check,
  ChevronRight,
  CircleSlash,
  Loader2,
  X,
} from "lucide-react";
import { cn } from "@/lib/cn";
import { formatCount, formatRelative } from "@/lib/format";
import type { PipelineRun, PipelineStep } from "@/lib/types";
import { LAYER_COLORS } from "@/components/datasets/LayerBadge";

const STATUS_ICON = {
  running: Loader2,
  succeeded: Check,
  failed: X,
  skipped: CircleSlash,
} as const;

const STATUS_COLOR = {
  running: "var(--text-subtle)",
  succeeded: "var(--color-positive)",
  failed: "var(--color-critical)",
  skipped: "var(--text-subtle)",
} as const;

const KIND_LABEL = {
  ingest: "Land in bronze",
  silver: "Refine to silver",
  gold: "Build gold",
} as const;

export function RunTimeline({ run }: { run: PipelineRun }) {
  const grouped = new Map<string, PipelineStep[]>();
  for (const step of run.steps) {
    const list = grouped.get(step.dataset_name) ?? [];
    list.push(step);
    grouped.set(step.dataset_name, list);
  }

  return (
    <div className="px-4 pb-4">
      <div className="surface-panel overflow-hidden">
        <header className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-[var(--line)] px-4 py-2.5">
          <StatusDot status={run.status} />
          <span className="text-[12.5px] font-medium capitalize text-[var(--text)]">
            {run.status}
          </span>
          <span className="text-[11.5px] text-[var(--text-subtle)]">
            {run.dataset_count} {run.dataset_count === 1 ? "dataset" : "datasets"} ·{" "}
            {run.engine} · started {formatRelative(run.started_at)}
          </span>
          {run.summary && run.summary.failures > 0 && (
            <span className="text-[11.5px] text-[var(--color-critical)]">
              {run.summary.failures} failed
            </span>
          )}
        </header>

        <ul>
          {[...grouped.entries()].map(([dataset, steps]) => (
            <li key={dataset} className="border-b border-[var(--line)] last:border-b-0">
              <div className="px-4 py-2">
                <p className="mb-1.5 text-[12.5px] font-medium text-[var(--text)]">{dataset}</p>
                <ol className="space-y-1">
                  {steps.map((step) => (
                    <StepRow key={step.id} step={step} />
                  ))}
                </ol>
              </div>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

function StatusDot({ status }: { status: PipelineRun["status"] }) {
  const color =
    status === "succeeded"
      ? "var(--color-positive)"
      : status === "failed"
        ? "var(--color-critical)"
        : status === "partial"
          ? "var(--color-caution)"
          : "var(--text-subtle)";
  return (
    <span
      className={cn("size-2 rounded-full", status === "running" && "animate-pulse")}
      style={{ background: color }}
      aria-hidden
    />
  );
}

function StepRow({ step }: { step: PipelineStep }) {
  const [open, setOpen] = useState(false);
  const Icon = STATUS_ICON[step.status];
  const color = STATUS_COLOR[step.status];
  const hasDetail = Boolean(step.actions?.length || step.notes?.length || step.sql || step.message);

  return (
    <li>
      <button
        onClick={() => hasDetail && setOpen((value) => !value)}
        className={cn(
          "flex w-full items-center gap-2 rounded-[var(--radius-sm)] px-2 py-1 text-left",
          hasDetail && "hover:bg-[var(--surface-hover)]",
        )}
      >
        <ChevronRight
          className={cn(
            "size-3 shrink-0 transition-transform",
            open && "rotate-90",
            !hasDetail && "invisible",
          )}
          style={{ color: "var(--text-subtle)" }}
          aria-hidden
        />
        <Icon
          className={cn("size-3 shrink-0", step.status === "running" && "animate-spin")}
          style={{ color }}
          aria-hidden
        />
        <span
          className="size-1.5 shrink-0 rounded-full"
          style={{ background: LAYER_COLORS[step.layer].color }}
          aria-hidden
        />
        <span className="text-[12px] text-[var(--text-muted)]">{KIND_LABEL[step.kind]}</span>
        {step.rows_out !== null && (
          <span className="text-[11.5px] tabular-nums text-[var(--text-subtle)]">
            {formatCount(step.rows_out)} rows
          </span>
        )}
        {step.target_table && (
          <span className="truncate font-mono text-[10.5px] text-[var(--text-subtle)]">
            {step.target_table.replace(/"/g, "")}
          </span>
        )}
        {step.duration_ms !== null && (
          <span className="ml-auto shrink-0 text-[10.5px] tabular-nums text-[var(--text-subtle)]">
            {step.duration_ms} ms
          </span>
        )}
      </button>

      {open && (
        <div className="ml-7 border-l border-[var(--line)] pb-1.5 pl-3">
          {step.message && (
            <p
              className="py-1 text-[11.5px] leading-relaxed"
              style={{ color: step.status === "failed" ? "var(--color-critical)" : "var(--text-muted)" }}
            >
              {step.message}
            </p>
          )}
          {step.actions?.map((action) => (
            <p key={action} className="py-px text-[11.5px] leading-relaxed text-[var(--text-muted)]">
              {action}
            </p>
          ))}
          {step.notes?.map((note) => (
            <p
              key={note}
              className="mt-1 rounded-[var(--radius-sm)] border border-[var(--color-caution)]/25 bg-[var(--color-caution)]/6 px-2 py-1 text-[11.5px] leading-relaxed text-[var(--text-muted)]"
            >
              {note}
            </p>
          ))}
          {step.sql && (
            <pre className="mt-1.5 overflow-x-auto rounded-[var(--radius-sm)] border border-[var(--line)] bg-[var(--surface-sunken)] px-2.5 py-2 font-mono text-[10.5px] leading-relaxed text-[var(--text-muted)]">
              {step.sql}
            </pre>
          )}
        </div>
      )}
    </li>
  );
}
