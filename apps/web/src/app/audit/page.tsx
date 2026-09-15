"use client";

import { useQuery } from "@tanstack/react-query";
import { Clock, Shield, ShieldAlert, ShieldCheck } from "lucide-react";
import { api } from "@/lib/api";
import { PageHeader } from "@/components/PageHeader";
import { Badge } from "@/components/ui/Badge";
import { EmptyState } from "@/components/ui/EmptyState";
import { cn } from "@/lib/cn";

interface AuditEvent {
  id: string;
  tenant_id: string;
  actor_id: string;
  action: string;
  target_type: string;
  target_id: string;
  details: Record<string, any>;
  timestamp: string;
}

function getActionConfig(action: string) {
  if (action.includes("delete") || action.includes("remove") || action.includes("drop")) {
    return { tone: "critical" as const, Icon: ShieldAlert };
  }
  if (action.includes("create") || action.includes("grant") || action.includes("add")) {
    return { tone: "positive" as const, Icon: ShieldCheck };
  }
  return { tone: "neutral" as const, Icon: Shield };
}

export default function AuditPage() {
  const { data: events, isLoading } = useQuery({
    queryKey: ["audit"],
    queryFn: () => api.get<AuditEvent[]>("/api/audit"),
    refetchInterval: 30_000,
  });

  return (
    <>
      <PageHeader
        title="Audit Log"
        description="Immutable record of security and administrative operations."
      />

      <div className="flex min-h-0 flex-1 flex-col overflow-y-auto p-4">
        {isLoading ? (
          <div className="flex flex-1 items-center justify-center text-[var(--text-muted)]">
            <Clock className="size-4 animate-spin" />
          </div>
        ) : events && events.length > 0 ? (
          <div className="flex flex-col gap-2">
            {events.map((event) => {
              const { tone, Icon } = getActionConfig(event.action);
              return (
                <div
                  key={event.id}
                  className="flex flex-col gap-3 rounded-[var(--radius-md)] border border-[var(--line)] bg-[var(--surface)] px-4 py-3"
                >
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-2.5">
                      <div
                        className={cn(
                          "flex size-7 items-center justify-center rounded-[var(--radius-sm)]",
                          tone === "critical" && "bg-[var(--color-critical)]/10 text-[var(--color-critical)]",
                          tone === "positive" && "bg-[var(--color-positive)]/10 text-[var(--color-positive)]",
                          tone === "neutral" && "bg-[var(--surface-sunken)] text-[var(--text-muted)]",
                        )}
                      >
                        <Icon className="size-3.5" aria-hidden />
                      </div>
                      <p className="text-[13px] font-medium text-[var(--text)]">
                        {event.action}
                      </p>
                      <Badge tone={tone}>{event.target_type}</Badge>
                    </div>
                    <time className="text-[11.5px] text-[var(--text-muted)]">
                      {new Date(event.timestamp).toLocaleString()}
                    </time>
                  </div>
                  
                  <div className="flex flex-col gap-1.5 pl-[38px] text-[12px] text-[var(--text-muted)]">
                    <div className="flex items-center gap-2">
                      <span className="font-medium text-[var(--text)]">Actor:</span>
                      <span className="font-mono">{event.actor_id}</span>
                    </div>
                    <div className="flex items-center gap-2">
                      <span className="font-medium text-[var(--text)]">Target ID:</span>
                      <span className="font-mono">{event.target_id}</span>
                    </div>
                    {Object.keys(event.details).length > 0 && (
                      <div className="mt-1 rounded border border-[var(--line)] bg-[var(--surface-sunken)] p-2">
                        <pre className="font-mono text-[11px] text-[var(--text)]">
                          {JSON.stringify(event.details, null, 2)}
                        </pre>
                      </div>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        ) : (
          <EmptyState
            icon={Shield}
            title="No audit events"
            description="Operational changes and security events will appear here."
          />
        )}
      </div>
    </>
  );
}
