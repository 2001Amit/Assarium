const COMPACT = new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 });
const PLAIN = new Intl.NumberFormat("en");

export function formatCount(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return value < 10_000 ? PLAIN.format(value) : COMPACT.format(value);
}

export function formatBytes(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let size = value;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size < 10 && unit > 0 ? size.toFixed(1) : Math.round(size)} ${units[unit]}`;
}

export function formatRelative(iso: string | null | undefined): string {
  if (!iso) return "never";
  const then = new Date(iso).getTime();
  const seconds = Math.round((Date.now() - then) / 1000);
  if (seconds < 60) return "just now";
  const rtf = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
  const table: [number, Intl.RelativeTimeFormatUnit][] = [
    [60, "minute"],
    [24, "hour"],
    [7, "day"],
    [4.35, "week"],
    [12, "month"],
  ];
  let value = seconds / 60;
  for (const [step, unit] of table) {
    if (Math.abs(value) < step) return rtf.format(-Math.round(value), unit);
    value /= step;
  }
  return rtf.format(-Math.round(value), "year");
}
