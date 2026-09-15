"use client";

import type {
  DashboardData,
  MetricFilter,
  SemanticMeasure,
  Tile as TileDefinition,
  TileResult,
} from "@/lib/types";
import { Tile } from "./Tile";

/**
 * The dashboard canvas.
 *
 * A 12-column grid; each tile declares its own span. Tiles are rendered from their
 * result rather than re-queried here, so the whole page reflects one consistent set
 * of filters rather than a patchwork of separately-timed reads.
 */
export function DashboardGrid({
  tiles,
  data,
  measures,
  filters,
  onCrossFilter,
  onDrill,
  onExplain,
  onExport,
  explanations,
  explaining,
}: {
  tiles: TileDefinition[];
  data: DashboardData;
  measures: SemanticMeasure[];
  filters: MetricFilter[];
  onCrossFilter: (field: string, label: string, raw: unknown) => void;
  onDrill: (tileId: string, dimension: string) => void;
  onExplain: (tileId: string) => void;
  onExport: (tileId: string) => void;
  explanations: Record<string, string>;
  explaining: string | null;
}) {
  const byId = new Map<string, TileResult>(data.tiles.map((t) => [t.tile_id, t]));

  return (
    <div className="grid grid-cols-12 gap-3 p-4">
      {tiles.map((tile) => {
        const result = byId.get(tile.id);
        if (!result) return null;

        // The value this tile is currently cross-filtered to, so the bar can show
        // as selected rather than the reader having to remember what they clicked.
        const activeValue =
          tile.dimension
            ? (filters.find(
                (f) => f.field === tile.dimension && f.operator === "eq",
              )?.values[0] as string | undefined) ?? null
            : null;

        return (
          <div
            key={tile.id}
            style={{
              gridColumn: `span ${Math.min(tile.width, 12)}`,
              minHeight: tile.type === "stat" ? 104 : tile.height * 132,
            }}
          >
            <Tile
              result={result}
              measures={measures}
              drillPath={tile.drill_path}
              activeDimension={tile.dimension}
              crossFilterValue={activeValue}
              onCrossFilter={
                tile.dimension
                  ? (label, raw) => onCrossFilter(tile.dimension as string, label, raw)
                  : undefined
              }
              onDrill={
                tile.drill_path.length > 0
                  ? (dimension) => onDrill(tile.id, dimension)
                  : undefined
              }
              onExplain={() => onExplain(tile.id)}
              onExport={() => onExport(tile.id)}
              explanation={explanations[tile.id] ?? null}
              explaining={explaining === tile.id}
            />
          </div>
        );
      })}
    </div>
  );
}
