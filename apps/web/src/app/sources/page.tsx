"use client";

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Plug, Search } from "lucide-react";
import { api } from "@/lib/api";
import type { Connection, SourceSpec } from "@/lib/types";
import { PageHeader } from "@/components/PageHeader";
import { ConnectDialog } from "@/components/connect/ConnectDialog";
import { ConnectionList } from "@/components/connect/ConnectionList";
import { CATEGORY_LABELS, CATEGORY_ORDER, sourceIcon } from "@/components/connect/sourceIcon";
import { EmptyState } from "@/components/ui/EmptyState";
import { Skeleton } from "@/components/ui/Skeleton";
import { inputClass } from "@/components/ui/Field";
import { cn } from "@/lib/cn";

export default function SourcesPage() {
  const [query, setQuery] = useState("");
  const [active, setActive] = useState<SourceSpec | null>(null);

  const { data: specs, isLoading } = useQuery({
    queryKey: ["sources"],
    queryFn: () => api.get<SourceSpec[]>("/api/sources"),
    staleTime: Infinity,
  });

  const { data: connections } = useQuery({
    queryKey: ["connections"],
    queryFn: () => api.get<Connection[]>("/api/connections"),
  });

  const grouped = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const matching = (specs ?? []).filter(
      (spec) =>
        !needle ||
        spec.name.toLowerCase().includes(needle) ||
        spec.summary.toLowerCase().includes(needle) ||
        spec.category.includes(needle),
    );
    return CATEGORY_ORDER.map((category) => ({
      category,
      items: matching.filter((spec) => spec.category === category),
    })).filter((group) => group.items.length > 0);
  }, [specs, query]);

  return (
    <>
      <PageHeader
        title="Sources"
        description="Connect the systems your data already lives in."
      />

      <div className="flex-1 overflow-y-auto">
        <div className="mx-auto max-w-5xl px-5 py-6">
          {connections && connections.length > 0 && (
            <section className="mb-8">
              <h2 className="mb-2.5 text-[12px] font-semibold uppercase tracking-[0.06em] text-[var(--text-subtle)]">
                Your connections
              </h2>
              <div className="surface-panel overflow-hidden">
                <ConnectionList specs={specs ?? []} />
              </div>
            </section>
          )}

          <section>
            <div className="mb-3 flex items-end justify-between gap-4">
              <div>
                <h2 className="text-[12px] font-semibold uppercase tracking-[0.06em] text-[var(--text-subtle)]">
                  Add a source
                </h2>
                <p className="mt-0.5 text-[12.5px] text-[var(--text-muted)]">
                  Pick a system. Assarium asks only for the credentials that system actually needs.
                </p>
              </div>
              <div className="relative w-56 shrink-0">
                <Search
                  className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-[var(--text-subtle)]"
                  aria-hidden
                />
                <input
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder="Search sources"
                  aria-label="Search sources"
                  className={cn(inputClass, "pl-8")}
                />
              </div>
            </div>

            {isLoading ? (
              <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
                {Array.from({ length: 6 }, (_, i) => (
                  <Skeleton key={i} className="h-[68px]" />
                ))}
              </div>
            ) : grouped.length === 0 ? (
              <div className="surface-panel">
                <EmptyState
                  icon={Plug}
                  title="No source matches that search"
                  description="Try a system name such as Postgres, Snowflake or SharePoint."
                />
              </div>
            ) : (
              <div className="space-y-6">
                {grouped.map((group) => (
                  <div key={group.category}>
                    <h3 className="mb-2 text-[11.5px] font-medium text-[var(--text-subtle)]">
                      {CATEGORY_LABELS[group.category]}
                    </h3>
                    <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
                      {group.items.map((spec) => {
                        const IconComponent = sourceIcon(spec.icon, spec.name);
                        // Extract brand color if available, default to primary accent
                        const brandColor = (IconComponent as any).brandColor || "#ffffff";
                        // Create inline styles for CSS variables to power dynamic styling
                        const style = {
                          "--brand": brandColor,
                          "--brand-bg": `${brandColor}15`, // 15% opacity tint for background
                          "--brand-border": `${brandColor}40`, // 25% opacity tint for border
                        } as React.CSSProperties;

                        return (
                          <button
                            key={spec.source_id}
                            onClick={() => setActive(spec)}
                            style={style}
                            className={cn(
                              "group flex gap-3.5 rounded-xl border border-[var(--line)] bg-[var(--surface)] p-3.5 text-left",
                              "shadow-[inset_0_1px_0_rgba(255,255,255,0.02)]",
                              "transition-all duration-300 ease-out hover:-translate-y-1 hover:shadow-[0_8px_24px_-8px_var(--brand-bg)] hover:border-[var(--brand-border)] hover:bg-[var(--surface-hover)]",
                            )}
                          >
                            <span 
                              className={cn(
                                "flex size-9 shrink-0 items-center justify-center rounded-lg border border-[var(--line)] bg-[var(--surface-sunken)] transition-colors duration-300",
                                "group-hover:border-[var(--brand-border)] group-hover:bg-[var(--brand-bg)]"
                              )}
                            >
                              <IconComponent
                                className="size-5 transition-transform duration-300 group-hover:scale-105"
                                aria-hidden
                              />
                            </span>
                            <span className="min-w-0 flex-1 py-0.5">
                              <span className="flex items-center justify-between gap-2">
                                <span className="truncate text-[13.5px] font-semibold tracking-tight text-[var(--text)]">
                                  {spec.name}
                                </span>
                                {!spec.available && (
                                  <span className="shrink-0 rounded-full bg-[var(--surface-sunken)] border border-[var(--line)] px-2 py-[2px] text-[10px] font-medium tracking-wide uppercase text-[var(--text-muted)]">
                                    Driver Needed
                                  </span>
                                )}
                              </span>
                              <span className="mt-1 line-clamp-2 block text-[12px] leading-relaxed text-[var(--text-subtle)]">
                                {spec.summary}
                              </span>
                            </span>
                          </button>
                        );
                      })}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </section>
        </div>
      </div>

      {active && (
        <ConnectDialog
          key={active.source_id}
          spec={active}
          open
          onClose={() => setActive(null)}
        />
      )}
    </>
  );
}
