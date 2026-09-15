"use client";

import { useEffect, useRef } from "react";
import { X } from "lucide-react";
import { cn } from "@/lib/cn";

interface DialogProps {
  open: boolean;
  onClose: () => void;
  title: string;
  description?: string;
  children: React.ReactNode;
  footer?: React.ReactNode;
  width?: "md" | "lg" | "xl";
}

const WIDTHS = { md: "max-w-lg", lg: "max-w-2xl", xl: "max-w-4xl" } as const;

export function Dialog({
  open,
  onClose,
  title,
  description,
  children,
  footer,
  width = "lg",
}: DialogProps) {
  const panelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    const previouslyFocused = document.activeElement as HTMLElement | null;
    document.body.style.overflow = "hidden";
    panelRef.current?.focus();
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = "";
      previouslyFocused?.focus?.();
    };
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto p-4 sm:p-8">
      <div
        className="fixed inset-0 bg-[var(--color-ink-950)]/45 backdrop-blur-[2px]"
        onClick={onClose}
        aria-hidden
      />
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        tabIndex={-1}
        className={cn(
          "fade-in relative z-10 my-auto w-full outline-none",
          "rounded-[var(--radius-xl)] border border-[var(--line)] bg-[var(--surface)]",
          "shadow-[0_1px_2px_rgba(0,0,0,0.04),0_16px_48px_-12px_rgba(0,0,0,0.28)]",
          WIDTHS[width],
        )}
      >
        <header className="flex items-start justify-between gap-4 border-b border-[var(--line)] px-5 py-3.5">
          <div className="min-w-0">
            <h2 className="text-[14px] font-semibold tracking-[-0.01em] text-[var(--text)]">
              {title}
            </h2>
            {description && (
              <p className="mt-0.5 text-[12.5px] text-[var(--text-muted)]">{description}</p>
            )}
          </div>
          <button
            onClick={onClose}
            aria-label="Close"
            className="-mr-1 -mt-0.5 rounded-[var(--radius-sm)] p-1 text-[var(--text-subtle)] transition-colors hover:bg-[var(--surface-hover)] hover:text-[var(--text)]"
          >
            <X className="size-4" aria-hidden />
          </button>
        </header>

        <div className="max-h-[min(62vh,640px)] overflow-y-auto px-5 py-4">{children}</div>

        {footer && (
          <footer className="flex items-center justify-end gap-2 border-t border-[var(--line)] bg-[var(--surface-sunken)] px-5 py-3 rounded-b-[var(--radius-xl)]">
            {footer}
          </footer>
        )}
      </div>
    </div>
  );
}
