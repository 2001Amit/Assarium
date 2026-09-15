import { cn } from "@/lib/cn";
import type { Layer } from "@/lib/types";

const LAYERS: Record<Layer, { label: string; color: string }> = {
  bronze: { label: "Bronze", color: "var(--color-bronze)" },
  silver: { label: "Silver", color: "var(--color-silver)" },
  gold: { label: "Gold", color: "var(--color-gold)" },
};

export function LayerBadge({
  layer,
  confidence,
  overridden,
  className,
}: {
  layer: Layer | null;
  confidence?: number | null;
  overridden?: boolean;
  className?: string;
}) {
  if (!layer) {
    return (
      <span className={cn("text-[11.5px] text-[var(--text-subtle)]", className)}>
        not profiled
      </span>
    );
  }
  const { label, color } = LAYERS[layer];
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-[var(--radius-xs)] border px-1.5 py-px",
        "text-[11px] font-medium leading-[1.45] whitespace-nowrap",
        className,
      )}
      style={{
        color,
        borderColor: `color-mix(in srgb, ${color} 30%, transparent)`,
        background: `color-mix(in srgb, ${color} 9%, transparent)`,
      }}
      title={
        overridden
          ? "Set manually. This overrides what the classifier detected."
          : confidence != null
            ? `Detected with ${Math.round(confidence * 100)}% confidence`
            : undefined
      }
    >
      <span className="size-1.5 rounded-full" style={{ background: color }} aria-hidden />
      {label}
      {overridden ? (
        <span className="opacity-70">set</span>
      ) : confidence != null ? (
        <span className="tabular-nums opacity-70">{Math.round(confidence * 100)}%</span>
      ) : null}
    </span>
  );
}

export const LAYER_COLORS = LAYERS;
