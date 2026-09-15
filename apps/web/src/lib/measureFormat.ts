import type { SemanticMeasure } from "./types";

/**
 * Render a measure value the way its definition says it should be read.
 *
 * Formatting lives with the measure rather than with each chart, so a currency total
 * looks the same in a table, a tile and a chat answer.
 */
export function formatMeasure(value: unknown, measure: SemanticMeasure): string {
  if (value === null || value === undefined) return "—";
  const numeric = typeof value === "number" ? value : Number(value);
  if (Number.isNaN(numeric)) return String(value);

  const options: Intl.NumberFormatOptions = {
    minimumFractionDigits: measure.decimals,
    maximumFractionDigits: measure.decimals,
  };

  if (measure.format === "currency") {
    // No currency symbol: the platform does not know which currency a column holds,
    // and guessing puts a wrong "$" in front of somebody's rupees.
    return new Intl.NumberFormat("en", options).format(numeric);
  }
  if (measure.format === "percent") {
    // Values already expressed as a percentage are not divided by 100 again.
    const scaled = Math.abs(numeric) <= 1 ? numeric * 100 : numeric;
    return `${new Intl.NumberFormat("en", options).format(scaled)}%`;
  }
  if (measure.format === "duration") {
    const seconds = Math.round(numeric);
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    return hours > 0 ? `${hours}h ${minutes}m` : `${minutes}m ${seconds % 60}s`;
  }
  return new Intl.NumberFormat("en", options).format(numeric);
}

/** Compact form for tiles and axis labels, where width is scarce. */
export function formatCompact(value: unknown, measure: SemanticMeasure): string {
  const numeric = typeof value === "number" ? value : Number(value);
  if (Number.isNaN(numeric)) return "—";
  if (measure.format === "percent") return formatMeasure(value, measure);
  if (Math.abs(numeric) < 10_000) return formatMeasure(value, measure);
  return new Intl.NumberFormat("en", {
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(numeric);
}

/**
 * Axis ticks.
 *
 * Ticks are round numbers chosen by the scale, not data points, so they never carry a
 * measure's decimals - a zero gridline reading "0.00" is noise. Percent keeps its sign
 * because the unit is the whole point.
 */
export function formatAxis(value: number, measure: SemanticMeasure | undefined): string {
  if (!Number.isFinite(value)) return "";
  if (measure?.format === "percent") {
    const scaled = Math.abs(value) <= 1 ? value * 100 : value;
    return `${new Intl.NumberFormat("en", { maximumFractionDigits: 1 }).format(scaled)}%`;
  }
  if (Math.abs(value) >= 10_000) {
    return new Intl.NumberFormat("en", {
      notation: "compact",
      maximumFractionDigits: 1,
    }).format(value);
  }
  return new Intl.NumberFormat("en", { maximumFractionDigits: 2 }).format(value);
}
