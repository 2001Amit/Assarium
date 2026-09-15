"use client";

import { useId } from "react";
import { cn } from "@/lib/cn";

interface FieldProps {
  label: string;
  help?: string | null;
  error?: string | null;
  required?: boolean;
  children: (id: string) => React.ReactNode;
  className?: string;
}

export function Field({ label, help, error, required, children, className }: FieldProps) {
  const id = useId();
  return (
    <div className={cn("flex flex-col gap-1.5", className)}>
      <label htmlFor={id} className="text-[12px] font-medium text-[var(--text-muted)]">
        {label}
        {!required && (
          <span className="ml-1.5 font-normal text-[var(--text-subtle)]">optional</span>
        )}
      </label>
      {children(id)}
      {error ? (
        <p className="text-[11.5px] text-[var(--color-critical)]">{error}</p>
      ) : help ? (
        <p className="text-[11.5px] leading-relaxed text-[var(--text-subtle)]">{help}</p>
      ) : null}
    </div>
  );
}

export const inputClass = cn(
  "w-full rounded-[var(--radius-md)] border border-[var(--line-strong)] bg-[var(--surface)]",
  "px-2.5 py-1.5 text-[13px] text-[var(--text)] placeholder:text-[var(--text-subtle)]",
  "transition-colors duration-100 outline-none",
  "focus:border-[var(--focus)] focus:ring-2 focus:ring-[var(--focus)]/20",
  "disabled:opacity-50",
);
