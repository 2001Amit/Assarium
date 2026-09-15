"use client";

import { useEffect, useMemo, useState } from "react";
import { cn } from "@/lib/cn";
import { inputClass } from "@/components/ui/Field";

interface CronPreset {
  label: string;
  value: string;
  description: string;
}

const PRESETS: CronPreset[] = [
  { label: "Every hour", value: "0 * * * *", description: "At the top of every hour" },
  { label: "Daily at 02:00", value: "0 2 * * *", description: "Every day at 02:00 UTC" },
  { label: "Daily at 06:00", value: "0 6 * * *", description: "Every day at 06:00 UTC" },
  { label: "Every 6 hours", value: "0 */6 * * *", description: "At 00:00, 06:00, 12:00, 18:00" },
  { label: "Weekly (Mon)", value: "0 2 * * 1", description: "Every Monday at 02:00 UTC" },
  { label: "Custom", value: "", description: "Enter a cron expression" },
];

/** Human-readable approximation of a 5-field cron expression. */
function describeCron(cron: string): string {
  const parts = cron.trim().split(/\s+/);
  if (parts.length !== 5) return "Invalid expression";

  const [minute, hour, dayOfMonth, month, dayOfWeek] = parts;

  // Exact preset matches
  if (cron === "0 * * * *") return "Every hour at :00";
  if (cron === "*/5 * * * *") return "Every 5 minutes";
  if (cron === "*/15 * * * *") return "Every 15 minutes";
  if (cron === "*/30 * * * *") return "Every 30 minutes";

  const days = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];

  if (month === "*" && dayOfMonth === "*") {
    const timeStr =
      hour !== "*" && minute !== "*"
        ? `at ${hour.padStart(2, "0")}:${minute.padStart(2, "0")} UTC`
        : hour !== "*"
          ? `at ${hour.padStart(2, "0")}:00 UTC`
          : "";

    if (dayOfWeek !== "*") {
      const dayIdx = parseInt(dayOfWeek, 10);
      const dayName = days[dayIdx] ?? dayOfWeek;
      return `Every ${dayName} ${timeStr}`.trim();
    }
    if (timeStr) return `Every day ${timeStr}`;
  }

  return cron;
}

/**
 * Compute the next N occurrences of a cron expression.
 * This is a simplified client-side preview — the server validates with croniter.
 */
function nextOccurrences(cron: string, count: number = 3): Date[] {
  const parts = cron.trim().split(/\s+/);
  if (parts.length !== 5) return [];

  const [minute, hour] = parts;
  const results: Date[] = [];
  const now = new Date();

  for (let i = 0; i < count * 24 && results.length < count; i++) {
    const candidate = new Date(now.getTime() + i * 3600_000);
    if (hour !== "*" && candidate.getUTCHours() !== parseInt(hour, 10)) continue;
    if (minute !== "*") candidate.setUTCMinutes(parseInt(minute, 10));
    if (candidate > now) results.push(candidate);
  }
  return results;
}

export function CronInput({
  value,
  onChange,
  error,
}: {
  value: string;
  onChange: (cron: string) => void;
  error?: string | null;
}) {
  const [mode, setMode] = useState<"preset" | "custom">(
    PRESETS.some((p) => p.value === value && p.value !== "") ? "preset" : "custom",
  );

  const description = useMemo(() => describeCron(value), [value]);
  const upcoming = useMemo(() => nextOccurrences(value), [value]);

  return (
    <div className="flex flex-col gap-2">
      {/* Preset buttons */}
      <div className="flex flex-wrap gap-1.5">
        {PRESETS.map((preset) => {
          const isActive = preset.value ? value === preset.value : mode === "custom";
          return (
            <button
              key={preset.label}
              type="button"
              onClick={() => {
                if (preset.value) {
                  setMode("preset");
                  onChange(preset.value);
                } else {
                  setMode("custom");
                }
              }}
              className={cn(
                "rounded-[var(--radius-sm)] border px-2 py-1 text-[11.5px] font-medium transition-colors",
                isActive
                  ? "border-[var(--accent)] bg-[var(--accent)]/10 text-[var(--accent)]"
                  : "border-[var(--line)] text-[var(--text-muted)] hover:bg-[var(--surface-hover)]",
              )}
            >
              {preset.label}
            </button>
          );
        })}
      </div>

      {/* Custom input */}
      {mode === "custom" && (
        <input
          type="text"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder="0 2 * * *"
          className={cn(inputClass, "font-mono text-[12.5px]")}
          spellCheck={false}
        />
      )}

      {/* Description */}
      {value && (
        <p className="text-[11.5px] text-[var(--text-muted)]">{description}</p>
      )}

      {/* Next occurrences preview */}
      {upcoming.length > 0 && (
        <div className="flex flex-col gap-0.5">
          <p className="text-[10.5px] font-medium uppercase tracking-[0.06em] text-[var(--text-subtle)]">
            Next runs
          </p>
          {upcoming.map((date, i) => (
            <p key={i} className="font-mono text-[11.5px] text-[var(--text-muted)]">
              {date.toLocaleString(undefined, {
                weekday: "short",
                month: "short",
                day: "numeric",
                hour: "2-digit",
                minute: "2-digit",
                timeZoneName: "short",
              })}
            </p>
          ))}
        </div>
      )}

      {/* Error */}
      {error && (
        <p className="text-[11.5px] text-[var(--color-critical)]">{error}</p>
      )}
    </div>
  );
}
