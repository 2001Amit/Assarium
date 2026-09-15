"use client";

import { forwardRef } from "react";
import { Loader2 } from "lucide-react";
import { cn } from "@/lib/cn";

type Variant = "primary" | "secondary" | "ghost" | "danger";
type Size = "sm" | "md";

interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
  loading?: boolean;
  icon?: React.ComponentType<{ className?: string }>;
}

const VARIANTS: Record<Variant, string> = {
  primary:
    "bg-[var(--accent)] text-[var(--accent-contrast)] border-transparent hover:brightness-110 active:brightness-95",
  secondary:
    "bg-[var(--surface)] text-[var(--text)] border-[var(--line-strong)] hover:bg-[var(--surface-hover)]",
  ghost:
    "bg-transparent text-[var(--text-muted)] border-transparent hover:bg-[var(--surface-hover)] hover:text-[var(--text)]",
  danger:
    "bg-transparent text-[var(--color-critical)] border-[var(--line-strong)] hover:bg-[var(--color-critical)] hover:text-white hover:border-transparent",
};

const SIZES: Record<Size, string> = {
  sm: "h-7 px-2.5 text-[12.5px] gap-1.5",
  md: "h-8 px-3 text-[13px] gap-2",
};

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { className, variant = "secondary", size = "md", loading, icon: Icon, children, disabled, ...props },
  ref,
) {
  return (
    <button
      ref={ref}
      disabled={disabled || loading}
      className={cn(
        "inline-flex items-center justify-center rounded-[var(--radius-md)] border font-medium",
        "transition-[background-color,color,border-color,filter] duration-100",
        "disabled:opacity-45 disabled:pointer-events-none whitespace-nowrap",
        VARIANTS[variant],
        SIZES[size],
        className,
      )}
      {...props}
    >
      {loading ? (
        <Loader2 className="size-3.5 animate-spin" aria-hidden />
      ) : Icon ? (
        <Icon className="size-3.5 shrink-0" aria-hidden />
      ) : null}
      {children}
    </button>
  );
});
