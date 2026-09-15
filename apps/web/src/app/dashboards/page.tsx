"use client";

import { Suspense, useCallback, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ChevronDown,
  Download,
  FileSpreadsheet,
  LayoutDashboard,
  Printer,
  RefreshCw,
  Sparkles,
  Trash2,
} from "lucide-react";
import { api, downloadFile, AssariumApiError } from "@/lib/api";
import { cn } from "@/lib/cn";
import type {
  KpiExplanation,
  Connection,
  Dashboard,
  DashboardData,
  DashboardSummary,
  MetricFilter,
  SemanticModel,
} from "@/lib/types";
import { PageHeader } from "@/components/PageHeader";
import { DashboardGrid } from "@/components/dashboards/DashboardGrid";
import { FilterBar } from "@/components/dashboards/FilterBar";
import { Button } from "@/components/ui/Button";
import { Callout } from "@/components/ui/Callout";
import { EmptyState } from "@/components/ui/EmptyState";
import { inputClass } from "@/components/ui/Field";

function DashboardWorkspace() {
  const params = useSearchParams();
  const queryClient = useQueryClient();
  const [connectionId, setConnectionId] = useState<string | null>(
    params.get("connection"),
  );
  const [activeDashboardId, setActiveDashboardId] = useState<string | null>(null);
  const [filters, setFilters] = useState<MetricFilter[]>([]);

  // Connections list.
  const { data: connections } = useQuery({
    queryKey: ["connections"],
    queryFn: () => api.get<Connection[]>("/api/connections"),
  });

  useEffect(() => {
    if (!connectionId && connections?.length) setConnectionId(connections[0].id);
  }, [connections, connectionId]);

  // Semantic model (needed for the filter bar).
  const { data: model } = useQuery({
    queryKey: ["semantic", connectionId],
    queryFn: () =>
      api.get<SemanticModel>(`/api/connections/${connectionId}/semantic`),
    enabled: Boolean(connectionId),
    retry: false,
  });

  // Dashboard list for this connection.
  const { data: dashboards } = useQuery({
    queryKey: ["dashboards", connectionId],
    queryFn: () =>
      api.get<DashboardSummary[]>(
        `/api/connections/${connectionId}/dashboards`,
      ),
    enabled: Boolean(connectionId),
  });

  // Auto-select the first dashboard.
  useEffect(() => {
    if (!activeDashboardId && dashboards?.length) {
      setActiveDashboardId(dashboards[0].id);
    }
  }, [dashboards, activeDashboardId]);

  // Active dashboard definition.
  const { data: dashboard } = useQuery({
    queryKey: ["dashboard", activeDashboardId],
    queryFn: () =>
      api.get<Dashboard>(`/api/dashboards/${activeDashboardId}`),
    enabled: Boolean(activeDashboardId),
  });

  // Dashboard data (re-fetched when filters change).
  const {
    data: dashboardData,
    isFetching: loadingData,
    refetch: refetchData,
  } = useQuery({
    queryKey: ["dashboard-data", activeDashboardId, filters],
    queryFn: () =>
      api.post<DashboardData>(`/api/dashboards/${activeDashboardId}/data`, {
        filters,
      }),
    enabled: Boolean(activeDashboardId),
  });

  // Generate dashboard.
  const generate = useMutation({
    mutationFn: () =>
      api.post<Dashboard>(
        `/api/connections/${connectionId}/dashboards/generate`,
      ),
    onSuccess: (newDashboard) => {
      queryClient.invalidateQueries({
        queryKey: ["dashboards", connectionId],
      });
      setActiveDashboardId(newDashboard.id);
    },
  });

  // --- interaction -------------------------------------------------------------------
  //
  // Cross-filtering, drill-down and explanations all operate on the dashboard's own
  // definition and filter set; none of them build a query by hand.

  const [explanations, setExplanations] = useState<Record<string, string>>({});
  const [explaining, setExplaining] = useState<string | null>(null);

  /** Clicking a bar filters the whole page to that value, or clears it if already set. */
  const crossFilter = useCallback(
    (field: string, label: string, raw: unknown) => {
      const value = raw === null || raw === undefined ? label : raw;
      setFilters((current) => {
        const existing = current.find(
          (f) => f.field === field && f.operator === "eq",
        );
        if (existing && String(existing.values[0]) === String(value)) {
          return current.filter((f) => f !== existing);
        }
        return [
          ...current.filter((f) => !(f.field === field && f.operator === "eq")),
          { field, operator: "eq", values: [value] },
        ];
      });
      setExplanations({});
    },
    [],
  );

  /** Drill-down swaps which dimension a tile breaks its measure down by. */
  const drill = useMutation({
    mutationFn: async ({ tileId, dimension }: { tileId: string; dimension: string }) => {
      if (!dashboard) return null;
      const next = {
        ...dashboard,
        tiles: dashboard.tiles.map((tile) =>
          tile.id === tileId
            ? {
                ...tile,
                dimension,
                title: `${tile.title.split(" by ")[0]} by ${dimension
                  .split(".")
                  .pop()}`,
                // The dimension just left becomes available to drill back into.
                drill_path: [
                  ...tile.drill_path.filter((d) => d !== dimension),
                  tile.dimension,
                ].filter((d): d is string => Boolean(d)),
              }
            : tile,
        ),
      };
      return api.put<Dashboard>(`/api/dashboards/${dashboard.id}`, next);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["dashboard", activeDashboardId] });
      queryClient.invalidateQueries({ queryKey: ["dashboard-data", activeDashboardId] });
    },
  });

  const explain = useCallback(
    async (tileId: string) => {
      if (!activeDashboardId) return;
      setExplaining(tileId);
      try {
        const result = await api.post<KpiExplanation>(
          `/api/dashboards/${activeDashboardId}/tiles/${tileId}/explain`,
          { filters },
        );
        setExplanations((current) => ({ ...current, [tileId]: result.explanation }));
      } catch (error) {
        setExplanations((current) => ({
          ...current,
          [tileId]:
            error instanceof AssariumApiError
              ? error.message
              : "The explanation could not be generated.",
        }));
      } finally {
        setExplaining(null);
      }
    },
    [activeDashboardId, filters],
  );

  /** Downloads go through the browser so the file lands where the user expects. */
  const exportTile = useCallback(
    async (tileId: string) => {
      if (!activeDashboardId) return;
      await downloadFile(
        `/api/dashboards/${activeDashboardId}/tiles/${tileId}/export.csv`,
        { filters },
      );
    },
    [activeDashboardId, filters],
  );

  const exportWorkbook = useCallback(async () => {
    if (!activeDashboardId) return;
    await downloadFile(`/api/dashboards/${activeDashboardId}/export.xlsx`, { filters });
  }, [activeDashboardId, filters]);

  // Delete dashboard.
  const deleteDashboard = useMutation({
    mutationFn: (id: string) => api.del(`/api/dashboards/${id}`),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["dashboards", connectionId],
      });
      setActiveDashboardId(null);
    },
  });

  // Export XLSX.
  const exportXlsx = useCallback(async () => {
    if (!activeDashboardId) return;
    const response = await fetch(
      `/api/dashboards/${activeDashboardId}/export.xlsx`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filters }),
      },
    );
    if (!response.ok) return;
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download =
      response.headers
        .get("Content-Disposition")
        ?.match(/filename="(.+)"/)?.[1] ?? "dashboard.xlsx";
    a.click();
    URL.revokeObjectURL(url);
  }, [activeDashboardId, filters]);

  // Export full HTML report.
  const exportReport = useCallback(() => {
    if (!dashboard || !dashboardData) return;
    _generateHtmlReport(dashboard, dashboardData, filters);
  }, [dashboard, dashboardData, filters]);

  const hasModel = Boolean(model);
  const hasDashboards = (dashboards?.length ?? 0) > 0;

  return (
    <>
      <PageHeader
        title="Dashboards"
        description="Auto-generated from the semantic model. Every number traces back to its definition."
        actions={
          <>
            {/* Connection picker */}
            <div className="relative">
              <select
                aria-label="Connection"
                value={connectionId ?? ""}
                onChange={(event) => {
                  setConnectionId(event.target.value);
                  setActiveDashboardId(null);
                  setFilters([]);
                }}
                className={cn(
                  inputClass,
                  "h-7 appearance-none py-0 pr-7 text-[12.5px]",
                )}
              >
                {connections?.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name}
                  </option>
                ))}
              </select>
              <ChevronDown
                className="pointer-events-none absolute right-2 top-1/2 size-3 -translate-y-1/2 text-[var(--text-subtle)]"
                aria-hidden
              />
            </div>

            {/* Dashboard picker */}
            {hasDashboards && (
              <div className="relative">
                <select
                  aria-label="Dashboard"
                  value={activeDashboardId ?? ""}
                  onChange={(e) => {
                    setActiveDashboardId(e.target.value);
                    setFilters([]);
                  }}
                  className={cn(
                    inputClass,
                    "h-7 appearance-none py-0 pr-7 text-[12.5px]",
                  )}
                >
                  {dashboards?.map((d) => (
                    <option key={d.id} value={d.id}>
                      {d.name} ({d.tile_count} tiles)
                    </option>
                  ))}
                </select>
                <ChevronDown
                  className="pointer-events-none absolute right-2 top-1/2 size-3 -translate-y-1/2 text-[var(--text-subtle)]"
                  aria-hidden
                />
              </div>
            )}

            {hasModel && (
              <Button
                size="sm"
                icon={Sparkles}
                loading={generate.isPending}
                onClick={() => generate.mutate()}
              >
                Generate
              </Button>
            )}

            {activeDashboardId && (
              <>
                <Button size="sm" icon={FileSpreadsheet} onClick={exportXlsx}>
                  XLSX
                </Button>
                <Button size="sm" icon={Printer} onClick={exportReport}>
                  Report
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  icon={Trash2}
                  loading={deleteDashboard.isPending}
                  onClick={() => {
                    if (confirm("Delete this dashboard?"))
                      deleteDashboard.mutate(activeDashboardId);
                  }}
                />
              </>
            )}
          </>
        }
      />

      {!hasModel ? (
        <EmptyState
          icon={LayoutDashboard}
          title="No semantic model yet"
          description="Build it from the pipeline to enable dashboards. Every chart is derived from the model's measures and dimensions."
        />
      ) : !hasDashboards ? (
        <EmptyState
          icon={LayoutDashboard}
          title="No dashboards"
          description="Generate one from the semantic model. The platform composes what the data supports — stat tiles, trends, breakdowns, and detail tables."
          action={
            <Button
              variant="primary"
              icon={Sparkles}
              loading={generate.isPending}
              onClick={() => generate.mutate()}
            >
              Generate dashboard
            </Button>
          }
        />
      ) : dashboard && dashboardData ? (
        <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">
          {/* Filter bar */}
          {model && connectionId && (
            <FilterBar
              model={model}
              filters={filters}
              onChange={setFilters}
              connectionId={connectionId}
            />
          )}

          {/* Notes */}
          {dashboard.notes.length > 0 && (
            <div className="space-y-1.5 px-4 pt-3">
              {dashboard.notes.map((note, i) => (
                <Callout key={i} tone="caution">
                  {note}
                </Callout>
              ))}
            </div>
          )}

          {/* Elapsed */}
          <div className="flex items-center justify-between px-4 pt-3">
            <p className="text-[11px] text-[var(--text-subtle)]">
              {dashboard.tiles.length} tile{dashboard.tiles.length !== 1 ? "s" : ""} ·{" "}
              {dashboardData.elapsed_ms}ms
            </p>
            {loadingData && (
              <RefreshCw className="size-3 animate-spin text-[var(--text-subtle)]" />
            )}
          </div>

          {/* Tile grid */}
          <DashboardGrid
            tiles={dashboard.tiles}
            data={dashboardData}
            measures={model?.measures ?? []}
            filters={filters}
            onCrossFilter={crossFilter}
            onDrill={(tileId, dimension) => drill.mutate({ tileId, dimension })}
            onExplain={explain}
            onExport={exportTile}
            explanations={explanations}
            explaining={explaining}
          />
        </div>
      ) : (
        <div className="flex flex-1 items-center justify-center">
          <RefreshCw className="size-4 animate-spin text-[var(--text-subtle)]" />
        </div>
      )}
    </>
  );
}

export default function DashboardsPage() {
  return (
    <Suspense fallback={<PageHeader title="Dashboards" />}>
      <DashboardWorkspace />
    </Suspense>
  );
}

// --------------------------------------------------------------------------------------
// Full HTML report generation (client-side)
// --------------------------------------------------------------------------------------

function _generateHtmlReport(
  dashboard: Dashboard,
  data: DashboardData,
  filters: MetricFilter[],
) {
  const resultMap = new Map(data.tiles.map((t) => [t.tile_id, t]));
  const now = new Date().toISOString().slice(0, 16).replace("T", " ");

  let tilesHtml = "";
  for (const tile of dashboard.tiles) {
    const result = resultMap.get(tile.id);
    if (!result) continue;
    if (result.error) {
      tilesHtml += `<div class="tile tile-error"><h3>${_esc(result.title)}</h3><p class="error">${_esc(result.error)}</p></div>`;
      continue;
    }

    if (result.type === "stat" && result.stats.length > 0) {
      const stat = result.stats[0];
      const delta =
        stat.delta !== null
          ? `<span class="delta ${stat.direction}">${stat.delta > 0 ? "+" : ""}${_fmtNum(stat.delta)}${stat.delta_pct !== null ? ` (${(stat.delta_pct * 100).toFixed(1)}%)` : ""}</span>`
          : "";
      tilesHtml += `<div class="tile tile-stat"><h3>${_esc(result.title)}</h3><p class="value">${stat.value !== null ? _fmtNum(stat.value) : "—"}</p>${delta}</div>`;
    } else if (result.rows.length > 0) {
      let table = `<table><thead><tr>${result.columns.map((c) => `<th>${_esc(c.replace(/_/g, " "))}</th>`).join("")}</tr></thead><tbody>`;
      for (const row of result.rows.slice(0, 100)) {
        table += `<tr>${row.map((cell) => `<td>${_esc(_fmtCell(cell))}</td>`).join("")}</tr>`;
      }
      table += "</tbody></table>";
      if (result.truncated || result.rows.length > 100) {
        table += `<p class="note">Showing ${Math.min(result.rows.length, 100)} of ${result.rows.length} rows</p>`;
      }
      tilesHtml += `<div class="tile"><h3>${_esc(result.title)}</h3>${table}</div>`;
    }
  }

  const filterHtml =
    filters.length > 0
      ? `<div class="filters"><strong>Filters:</strong> ${filters.map((f) => `${_esc(f.field)} ${_esc(f.operator)} ${f.values.map(String).join(", ")}`).join(" · ")}</div>`
      : "";

  const html = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>${_esc(dashboard.name)} — Assarium Report</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Inter',system-ui,sans-serif;font-size:13px;line-height:1.5;color:#26272c;background:#f7f7f8;-webkit-print-color-adjust:exact;print-color-adjust:exact}
.report{max-width:1100px;margin:0 auto;padding:32px 24px}
header{border-bottom:1px solid #e3e3e7;padding-bottom:16px;margin-bottom:24px}
header h1{font-size:20px;font-weight:600;letter-spacing:-0.02em}
header p{font-size:12px;color:#5e6068;margin-top:2px}
.filters{font-size:11px;color:#5e6068;margin-bottom:16px;padding:8px 12px;background:#fff;border:1px solid #e3e3e7;border-radius:6px}
.tile{background:#fff;border:1px solid #e3e3e7;border-radius:8px;padding:16px;margin-bottom:16px;page-break-inside:avoid}
.tile h3{font-size:13px;font-weight:600;margin-bottom:8px;color:#26272c}
.tile-stat .value{font-size:28px;font-weight:700;font-variant-numeric:tabular-nums;letter-spacing:-0.03em;color:#26272c}
.tile-stat .delta{display:inline-block;margin-top:4px;font-size:12px;font-weight:500}
.delta.up{color:#2f7d5e}
.delta.down{color:#b4453a}
.delta.flat{color:#8a8c95}
.tile-error .error{color:#b4453a;font-size:12px}
table{width:100%;border-collapse:collapse;font-size:12px;font-variant-numeric:tabular-nums}
th{text-align:left;padding:6px 10px;background:#f7f7f8;border-bottom:1px solid #e3e3e7;font-weight:600;font-size:10px;text-transform:uppercase;letter-spacing:0.04em;color:#8a8c95}
td{padding:5px 10px;border-bottom:1px solid #eeeef0}
tr:last-child td{border-bottom:none}
.note{font-size:10px;color:#8a8c95;margin-top:6px}
footer{margin-top:32px;padding-top:12px;border-top:1px solid #e3e3e7;font-size:10px;color:#8a8c95;text-align:center}
@media print{body{background:#fff}.report{padding:12px}@page{margin:1cm;size:A4 landscape}}
</style>
</head>
<body>
<div class="report">
<header>
<h1>${_esc(dashboard.name)}</h1>
<p>Generated ${now} · ${data.tiles.length} tiles · ${data.elapsed_ms}ms</p>
${dashboard.description ? `<p>${_esc(dashboard.description)}</p>` : ""}
</header>
${filterHtml}
${tilesHtml}
<footer>Assarium — Self-Serve Data Platform</footer>
</div>
</body>
</html>`;

  const blob = new Blob([html], { type: "text/html" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${dashboard.name.replace(/[^a-zA-Z0-9 _-]/g, "").replace(/ /g, "-").toLowerCase()}-report.html`;
  a.click();
  URL.revokeObjectURL(url);
}

function _esc(str: string): string {
  return str
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function _fmtNum(v: number): string {
  if (Math.abs(v) >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`;
  if (Math.abs(v) >= 1_000) return `${(v / 1_000).toFixed(1)}K`;
  if (Number.isInteger(v)) return new Intl.NumberFormat("en").format(v);
  return v.toFixed(2);
}

function _fmtCell(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "number") return _fmtNum(value);
  const str = String(value);
  if (str.endsWith("T00:00:00") || str.endsWith(" 00:00:00")) return str.slice(0, 10);
  return str;
}
