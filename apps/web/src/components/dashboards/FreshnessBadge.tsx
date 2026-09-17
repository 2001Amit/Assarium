import type { DashboardFreshness, FreshnessStatus } from "@/lib/types";
import { formatRelative } from "@/lib/format";
import { Badge } from "@/components/ui/Badge";

// "fresh" and "unknown" are both unremarkable and share a tone on purpose: a dashboard
// with nothing to judge against must not look like a problem, and must not look like a
// clean bill of health either. The wording separates them; the colour does not pretend to.
const TONE: Record<FreshnessStatus, "neutral" | "caution" | "critical"> = {
  fresh: "neutral",
  late: "caution",
  stale: "critical",
  unknown: "neutral",
};

export function FreshnessBadge({ freshness }: { freshness: DashboardFreshness }) {
  const { status, finished_at, reason } = freshness;

  // The verdict belongs to the server, which owns both the clock and the schedule. Only
  // the phrasing of the age is the browser's, so a skewed client clock can reword the
  // label but can never turn a stale dashboard into one that looks current.
  const label = finished_at ? `Updated ${formatRelative(finished_at)}` : "Never updated";

  return (
    <span className="flex min-w-0 items-center gap-2">
      <Badge tone={TONE[status]} title={reason ?? undefined}>
        {label}
      </Badge>
      {reason && (
        <span className="truncate text-[11px] text-[var(--text-subtle)]" title={reason}>
          {reason}
        </span>
      )}
    </span>
  );
}
