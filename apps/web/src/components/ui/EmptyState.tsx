import { cn } from "@/lib/cn";

export function EmptyState({
  icon: Icon,
  title,
  description,
  action,
  className,
}: {
  icon: React.ComponentType<{ className?: string }>;
  title: string;
  description?: string;
  action?: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center px-6 py-14 text-center",
        className,
      )}
    >
      <div className="mb-3 flex size-9 items-center justify-center rounded-[var(--radius-md)] border border-[var(--line)] bg-[var(--surface-sunken)]">
        <Icon className="size-4 text-[var(--text-subtle)]" aria-hidden />
      </div>
      <p className="text-[13.5px] font-medium text-[var(--text)]">{title}</p>
      {description && (
        <p className="mt-1 max-w-sm text-[12.5px] leading-relaxed text-[var(--text-muted)]">
          {description}
        </p>
      )}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}
