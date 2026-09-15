import { cn } from "@/lib/cn";

type Tone = "neutral" | "positive" | "caution" | "critical" | "info" | "accent";

const TONES: Record<Tone, string> = {
  neutral: "text-[var(--text-muted)] border-[var(--line-strong)] bg-[var(--surface-sunken)]",
  positive: "text-[var(--color-positive)] border-[var(--color-positive)]/25 bg-[var(--color-positive)]/8",
  caution: "text-[var(--color-caution)] border-[var(--color-caution)]/25 bg-[var(--color-caution)]/8",
  critical: "text-[var(--color-critical)] border-[var(--color-critical)]/25 bg-[var(--color-critical)]/8",
  info: "text-[var(--color-info)] border-[var(--color-info)]/25 bg-[var(--color-info)]/8",
  accent: "text-[var(--accent)] border-[var(--accent)]/25 bg-[var(--accent)]/8",
};

export function Badge({
  tone = "neutral",
  className,
  title,
  children,
}: {
  tone?: Tone;
  className?: string;
  /** Hover explanation, for badges that compress a judgement into one word. */
  title?: string;
  children: React.ReactNode;
}) {
  return (
    <span
      title={title}
      className={cn(
        "inline-flex items-center gap-1 rounded-[var(--radius-xs)] border px-1.5 py-px",
        "text-[11px] font-medium leading-[1.45] whitespace-nowrap",
        TONES[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}
