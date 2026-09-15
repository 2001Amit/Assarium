"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyRound, ShieldAlert, TriangleAlert, X } from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { formatCount } from "@/lib/format";
import type { ColumnProfile, DatasetDetail as Detail, Layer, LayerSignal } from "@/lib/types";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Callout } from "@/components/ui/Callout";
import { Skeleton } from "@/components/ui/Skeleton";
import { LAYER_COLORS, LayerBadge } from "./LayerBadge";

const VERDICT_LABEL: Record<string, string> = {
  bronze: "argues raw",
  silver: "argues conformed",
  gold: "argues business-ready",
  neutral: "context",
};

export function DatasetDetailPanel({
  datasetId,
  onClose,
}: {
  datasetId: string;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();

  const { data, isLoading } = useQuery({
    queryKey: ["dataset", datasetId],
    queryFn: () => api.get<Detail>(`/api/datasets/${datasetId}`),
  });

  const profile = useMutation({
    mutationFn: () => api.post<Detail>(`/api/datasets/${datasetId}/profile`),
    onSuccess: (updated) => {
      queryClient.setQueryData(["dataset", datasetId], updated);
      queryClient.invalidateQueries({ queryKey: ["datasets"] });
    },
  });

  const override = useMutation({
    mutationFn: (layer: Layer | null) =>
      api.patch<Detail>(`/api/datasets/${datasetId}`, { layer }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["dataset", datasetId] });
      queryClient.invalidateQueries({ queryKey: ["datasets"] });
    },
  });

  return (
    <aside className="flex w-[420px] shrink-0 flex-col border-l border-[var(--line)] bg-[var(--surface)]">
      <header className="flex shrink-0 items-start justify-between gap-3 border-b border-[var(--line)] px-4 py-3">
        <div className="min-w-0">
          <h2 className="truncate text-[13px] font-semibold text-[var(--text)]">
            {data?.name ?? "Dataset"}
          </h2>
          <p className="truncate font-mono text-[11px] text-[var(--text-subtle)]">
            {data?.path.join(" / ")}
          </p>
        </div>
        <button
          onClick={onClose}
          aria-label="Close details"
          className="-mr-1 rounded-[var(--radius-sm)] p-1 text-[var(--text-subtle)] hover:bg-[var(--surface-hover)] hover:text-[var(--text)]"
        >
          <X className="size-4" aria-hidden />
        </button>
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto">
        {isLoading || !data ? (
          <div className="space-y-2 p-4">
            {Array.from({ length: 8 }, (_, i) => (
              <Skeleton key={i} className="h-7" />
            ))}
          </div>
        ) : (
          <>
            <section className="border-b border-[var(--line)] px-4 py-3">
              <div className="mb-2 flex items-center justify-between gap-2">
                <p className="text-[11px] font-semibold uppercase tracking-[0.06em] text-[var(--text-subtle)]">
                  Medallion layer
                </p>
                <Button
                  size="sm"
                  loading={profile.isPending}
                  onClick={() => profile.mutate()}
                >
                  {data.profile ? "Re-profile" : "Profile"}
                </Button>
              </div>

              {!data.layer_evidence ? (
                <p className="text-[12.5px] leading-relaxed text-[var(--text-muted)]">
                  Profile this dataset to work out which layer its data is already at.
                </p>
              ) : (
                <LayerVerdictView
                  verdict={data.layer_evidence}
                  override={data.layer_override}
                  onOverride={(layer) => override.mutate(layer)}
                />
              )}
            </section>

            {data.profile && <ProfileSummary detail={data} />}
            {data.profile && <ColumnTable columns={data.profile.columns} />}
          </>
        )}
      </div>
    </aside>
  );
}

function LayerVerdictView({
  verdict,
  override,
  onOverride,
}: {
  verdict: NonNullable<Detail["layer_evidence"]>;
  override: Layer | null;
  onOverride: (layer: Layer | null) => void;
}) {
  return (
    <div>
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <LayerBadge
          layer={override ?? verdict.layer}
          confidence={verdict.confidence}
          overridden={Boolean(override)}
        />
        <span className="text-[12.5px] text-[var(--text-muted)]">{verdict.summary}</span>
      </div>

      {override && override !== verdict.layer && (
        <Callout tone="info" className="mb-2">
          You set this to {override}; the classifier read it as {verdict.layer}.{" "}
          <button
            onClick={() => onOverride(null)}
            className="underline underline-offset-2 hover:text-[var(--accent)]"
          >
            Use the detected layer
          </button>
        </Callout>
      )}

      <p className="mb-1.5 text-[11px] font-medium text-[var(--text-subtle)]">
        Evidence used
      </p>
      <ul className="mb-3 space-y-1">
        {verdict.signals.map((signal) => (
          <SignalRow key={signal.id} signal={signal} />
        ))}
      </ul>

      <p className="mb-1.5 text-[11px] font-medium text-[var(--text-subtle)]">
        Set the layer yourself
      </p>
      <div className="flex gap-1.5">
        {(["bronze", "silver", "gold"] as Layer[]).map((layer) => {
          const active = override === layer;
          return (
            <button
              key={layer}
              onClick={() => onOverride(active ? null : layer)}
              className={cn(
                "flex-1 rounded-[var(--radius-md)] border px-2 py-1 text-[12px] font-medium capitalize transition-colors",
                active
                  ? "border-transparent text-[var(--surface)]"
                  : "border-[var(--line-strong)] text-[var(--text-muted)] hover:bg-[var(--surface-hover)]",
              )}
              style={active ? { background: LAYER_COLORS[layer].color } : undefined}
            >
              {layer}
            </button>
          );
        })}
      </div>
      <p className="mt-2 text-[11.5px] leading-relaxed text-[var(--text-subtle)]">
        {verdict.recommended_action}
      </p>
    </div>
  );
}

function SignalRow({ signal }: { signal: LayerSignal }) {
  const color =
    signal.verdict === "neutral"
      ? "var(--text-subtle)"
      : LAYER_COLORS[signal.verdict as Layer].color;
  return (
    <li className="flex gap-2 rounded-[var(--radius-sm)] border border-[var(--line)] px-2 py-1.5">
      <span
        className="mt-[5px] size-1.5 shrink-0 rounded-full"
        style={{ background: color }}
        aria-hidden
      />
      <div className="min-w-0 flex-1">
        <p className="flex items-baseline gap-1.5">
          <span className="text-[12px] font-medium text-[var(--text)]">{signal.label}</span>
          <span className="text-[10.5px]" style={{ color }}>
            {VERDICT_LABEL[signal.verdict]}
          </span>
          <span className="ml-auto shrink-0 text-[10.5px] tabular-nums text-[var(--text-subtle)]">
            {signal.weight.toFixed(1)}
          </span>
        </p>
        <p className="text-[11.5px] leading-relaxed text-[var(--text-muted)]">
          {signal.observation}
        </p>
        {signal.detail && (
          <p className="mt-0.5 font-mono text-[10.5px] leading-relaxed text-[var(--text-subtle)]">
            {signal.detail}
          </p>
        )}
      </div>
    </li>
  );
}

function ProfileSummary({ detail }: { detail: Detail }) {
  const profile = detail.profile!;
  const stats = [
    {
      label: "Rows",
      value: formatCount(profile.row_count),
      note: profile.row_count_exact ? "exact" : "estimated",
    },
    { label: "Columns", value: String(profile.columns.length) },
    {
      label: "Duplicate rows",
      value: `${(profile.duplicate_row_ratio * 100).toFixed(1)}%`,
      warn: profile.duplicate_row_ratio > 0.01,
    },
    { label: "Sampled", value: formatCount(profile.sampled_rows) },
  ];

  return (
    <section className="border-b border-[var(--line)] px-4 py-3">
      <dl className="grid grid-cols-2 gap-x-4 gap-y-2">
        {stats.map((stat) => (
          <div key={stat.label}>
            <dt className="text-[11px] text-[var(--text-subtle)]">{stat.label}</dt>
            <dd
              className={cn(
                "text-[13px] font-medium tabular-nums",
                stat.warn ? "text-[var(--color-caution)]" : "text-[var(--text)]",
              )}
            >
              {stat.value}
              {stat.note && (
                <span className="ml-1 text-[10.5px] font-normal text-[var(--text-subtle)]">
                  {stat.note}
                </span>
              )}
            </dd>
          </div>
        ))}
      </dl>

      {profile.key_candidates.length > 0 && (
        <div className="mt-3 flex flex-wrap items-center gap-1.5">
          <KeyRound className="size-3 text-[var(--text-subtle)]" aria-hidden />
          <span className="text-[11px] text-[var(--text-subtle)]">Identifies a row:</span>
          {profile.key_candidates.map((key) => (
            <Badge key={key.join("+")} tone="info">
              {key.join(" + ")}
            </Badge>
          ))}
        </div>
      )}

      {profile.warnings.map((warning) => (
        <Callout key={warning} tone="caution" className="mt-2">
          {warning}
        </Callout>
      ))}
    </section>
  );
}

function ColumnTable({ columns }: { columns: ColumnProfile[] }) {
  return (
    <section className="px-4 py-3">
      <p className="mb-2 text-[11px] font-semibold uppercase tracking-[0.06em] text-[var(--text-subtle)]">
        Columns
      </p>
      <ul className="space-y-1">
        {columns.map((column) => (
          <li
            key={column.name}
            className="rounded-[var(--radius-md)] border border-[var(--line)] px-2.5 py-2"
          >
            <div className="flex items-baseline gap-2">
              <span className="truncate text-[12.5px] font-medium text-[var(--text)]">
                {column.name}
              </span>
              <span className="shrink-0 font-mono text-[10.5px] text-[var(--text-subtle)]">
                {column.native_type}
              </span>
              {column.is_unique && (
                <KeyRound className="size-3 shrink-0 text-[var(--color-info)]" aria-hidden />
              )}
              <span className="ml-auto shrink-0 text-[10.5px] tabular-nums text-[var(--text-subtle)]">
                {column.distinct_count != null
                  ? `${formatCount(column.distinct_count)} distinct`
                  : ""}
              </span>
            </div>

            {/* Completeness as a single hairline bar: filled, blank, missing. */}
            <div className="mt-1.5 flex h-1 overflow-hidden rounded-full bg-[var(--surface-sunken)]">
              <span
                className="bg-[var(--color-positive)]"
                style={{ width: `${Math.max(0, (1 - column.null_pct) * 100)}%` }}
              />
              <span
                className="bg-[var(--color-critical)]"
                style={{ width: `${column.null_pct * 100}%` }}
              />
            </div>

            <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
              {column.null_pct > 0 && (
                <span className="text-[10.5px] tabular-nums text-[var(--text-subtle)]">
                  {(column.null_pct * 100).toFixed(1)}% missing
                </span>
              )}
              {column.blank_count > 0 && (
                <Badge tone="caution">{column.blank_count} blank</Badge>
              )}
              {column.is_constant && <Badge tone="neutral">single value</Badge>}
              {column.shadow_type && (
                <Badge tone="caution">
                  <TriangleAlert className="size-2.5" aria-hidden />
                  text holding {column.shadow_type}
                </Badge>
              )}
              {column.pii && (
                <Badge tone="critical" title={`Basis: ${column.pii.basis}`}>
                  <ShieldAlert className="size-2.5" aria-hidden />
                  {column.pii.kind.replace(/_/g, " ")}
                  <span className="opacity-70">
                    {Math.round(column.pii.confidence * 100)}%
                  </span>
                </Badge>
              )}
            </div>

            {(column.minimum || column.mean != null) && (
              <p className="mt-1 font-mono text-[10.5px] text-[var(--text-subtle)]">
                {column.mean != null
                  ? `min ${column.minimum} · p50 ${column.p50} · max ${column.maximum} · mean ${column.mean}`
                  : `min ${column.minimum} · max ${column.maximum}`}
              </p>
            )}

            {column.top_values.length > 0 && (
              <div className="mt-1.5 space-y-0.5">
                {column.top_values.slice(0, 4).map((top) => (
                  <div key={top.value} className="flex items-center gap-2">
                    <span className="w-24 shrink-0 truncate text-[10.5px] text-[var(--text-muted)]">
                      {top.value}
                    </span>
                    <span className="h-1 flex-1 rounded-full bg-[var(--surface-sunken)]">
                      <span
                        className="block h-full rounded-full bg-[var(--accent)]/50"
                        style={{ width: `${top.pct * 100}%` }}
                      />
                    </span>
                    <span className="w-9 shrink-0 text-right text-[10.5px] tabular-nums text-[var(--text-subtle)]">
                      {(top.pct * 100).toFixed(0)}%
                    </span>
                  </div>
                ))}
              </div>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
