import { AlertTriangle, CheckCircle2, Info, XCircle } from "lucide-react";
import { cn } from "@/lib/cn";

type Tone = "info" | "positive" | "caution" | "critical";

const CONFIG: Record<Tone, { icon: typeof Info; color: string; bg: string }> = {
  info: { icon: Info, color: "var(--color-info)", bg: "var(--color-info)" },
  positive: { icon: CheckCircle2, color: "var(--color-positive)", bg: "var(--color-positive)" },
  caution: { icon: AlertTriangle, color: "var(--color-caution)", bg: "var(--color-caution)" },
  critical: { icon: XCircle, color: "var(--color-critical)", bg: "var(--color-critical)" },
};

export function Callout({
  tone = "info",
  title,
  children,
  className,
}: {
  tone?: Tone;
  title?: string;
  children?: React.ReactNode;
  className?: string;
}) {
  const { icon: Icon, color, bg } = CONFIG[tone];
  return (
    <div
      className={cn(
        "flex gap-2.5 rounded-[var(--radius-md)] border px-3 py-2.5 text-[12.5px] leading-relaxed",
        className,
      )}
      style={{ borderColor: `color-mix(in srgb, ${color} 25%, transparent)`, background: `color-mix(in srgb, ${bg} 7%, transparent)` }}
    >
      <Icon className="mt-px size-3.5 shrink-0" style={{ color }} aria-hidden />
      <div className="min-w-0 flex-1">
        {title && <p className="font-medium text-[var(--text)]">{title}</p>}
        {children && <div className="text-[var(--text-muted)]">{children}</div>}
      </div>
    </div>
  );
}
