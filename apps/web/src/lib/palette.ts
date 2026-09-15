/**
 * Chart colour assignment.
 *
 * The slot order was validated with the dataviz validator in both light and dark
 * modes; it is the colourblind-safety mechanism, not a preference. Colours are
 * assigned by fixed slot and never cycled — a ninth series folds into "Other"
 * rather than inventing a hue that is indistinguishable under CVD.
 */
export const SERIES_SLOTS = 8;

export function seriesColor(index: number): string {
  return `var(--series-${Math.min(index, SERIES_SLOTS - 1) + 1})`;
}

/**
 * Colour follows the entity, never its rank.
 *
 * A filter that removes a series must not repaint the survivors, so the slot is
 * derived from a stable key rather than the current row order.
 */
export function stableSeriesColor(key: string, keys: string[]): string {
  const index = keys.indexOf(key);
  return seriesColor(index < 0 ? 0 : index);
}

/** Past this many series, fold the tail rather than adding hues. */
export const SERIES_CAP = 8;
