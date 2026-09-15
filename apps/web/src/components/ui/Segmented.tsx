"use client";

import { cn } from "@/lib/cn";

export interface SegmentedOption<T extends string> {
  value: T;
  label: string;
  hint?: string;
  disabled?: boolean;
}

export function Segmented<T extends string>({
  value,
  options,
  onChange,
  className,
}: {
  value: T;
  options: SegmentedOption<T>[];
  onChange: (value: T) => void;
  className?: string;
}) {
  return (
    <div
      role="tablist"
      className={cn(
        "inline-flex rounded-[var(--radius-md)] border border-[var(--line-strong)] bg-[var(--surface-sunken)] p-0.5",
        className,
      )}
    >
      {options.map((option) => {
        const active = option.value === value;
        return (
          <button
            key={option.value}
            role="tab"
            aria-selected={active}
            disabled={option.disabled}
            onClick={() => onChange(option.value)}
            className={cn(
              "rounded-[var(--radius-sm)] px-2.5 py-1 text-[12.5px] font-medium transition-colors duration-100",
              "disabled:opacity-40 disabled:pointer-events-none",
              active
                ? "bg-[var(--surface)] text-[var(--text)] shadow-[0_1px_2px_rgba(0,0,0,0.06)]"
                : "text-[var(--text-muted)] hover:text-[var(--text)]",
            )}
          >
            {option.label}
          </button>
        );
      })}
    </div>
  );
}
