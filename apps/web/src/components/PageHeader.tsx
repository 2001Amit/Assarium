export function PageHeader({
  title,
  description,
  actions,
}: {
  title: string;
  description?: string;
  actions?: React.ReactNode;
}) {
  return (
    <header className="flex h-12 shrink-0 items-center justify-between gap-6 border-b border-[var(--line)] bg-[var(--surface)] px-5">
      <div className="min-w-0">
        <h1 className="text-[13.5px] font-semibold tracking-[-0.01em] text-[var(--text)]">
          {title}
        </h1>
        {description && (
          <p className="truncate text-[12px] text-[var(--text-muted)]">{description}</p>
        )}
      </div>
      {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
    </header>
  );
}
