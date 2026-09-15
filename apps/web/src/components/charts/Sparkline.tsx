"use client";

/**
 * A sparkline is context for a headline number, not a chart in its own right.
 * No axes, no labels, no tooltip - the stat tile's value and delta carry the data,
 * and the full series is reachable from the tile's table view.
 */
export function Sparkline({
  values,
  color = "var(--series-1)",
  width = 108,
  height = 26,
}: {
  values: (number | null)[];
  color?: string;
  width?: number;
  height?: number;
}) {
  const points = values.filter((v): v is number => v !== null);
  if (points.length < 2) return null;

  const min = Math.min(...points);
  const max = Math.max(...points);
  const span = max - min || 1;
  // Inset by the marker radius so the end dot is never clipped by the viewBox.
  const pad = 3;
  const step = (width - pad * 2) / (values.length - 1);

  const coords = values.map((value, index) => {
    const x = pad + index * step;
    const y =
      value === null
        ? null
        : pad + (height - pad * 2) * (1 - (value - min) / span);
    return { x, y };
  });

  const drawn = coords.filter((c): c is { x: number; y: number } => c.y !== null);
  const path = drawn.map((c, i) => `${i === 0 ? "M" : "L"}${c.x},${c.y}`).join(" ");
  const last = drawn[drawn.length - 1];

  return (
    <svg
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      aria-hidden
      className="overflow-visible"
    >
      <path
        d={`${path} L${last.x},${height} L${drawn[0].x},${height} Z`}
        fill={color}
        opacity={0.1}
      />
      <path
        d={path}
        fill="none"
        stroke={color}
        strokeWidth={2}
        strokeLinejoin="round"
        strokeLinecap="round"
      />
      {/* Surface ring keeps the end marker legible where it crosses the line. */}
      <circle cx={last.x} cy={last.y} r={4} fill={color} stroke="var(--surface)" strokeWidth={2} />
    </svg>
  );
}
