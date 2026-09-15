"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import {
  Boxes,
  GitBranch,
  Layers,
  LayoutDashboard,
  MessagesSquare,
  Moon,
  Plug,
  Share2,
  Shield,
  Sun,
  Table2,
  Timer,
} from "lucide-react";
import { cn } from "@/lib/cn";

interface NavItem {
  href: string;
  label: string;
  icon: React.ComponentType<{ className?: string }>;
  /** Phases still to be built are shown but not reachable, so the shape of the product is legible. */
  ready?: boolean;
}

const NAV: { section: string; items: NavItem[] }[] = [
  {
    section: "Connect",
    items: [
      { href: "/sources", label: "Sources", icon: Plug, ready: true },
      { href: "/datasets", label: "Datasets", icon: Table2, ready: true },
    ],
  },
  {
    section: "Refine",
    items: [
      { href: "/pipeline", label: "Pipeline", icon: Layers, ready: true },
      { href: "/schedules", label: "Schedules", icon: Timer, ready: true },
      { href: "/ontology", label: "Ontology", icon: Share2, ready: true },
      { href: "/lineage", label: "Lineage", icon: GitBranch, ready: true },
      { href: "/model", label: "Semantic model", icon: Boxes, ready: true },
    ],
  },
  {
    section: "Security",
    items: [
      { href: "/audit", label: "Audit Log", icon: Shield, ready: true },
    ],
  },
  {
    section: "Analyse",
    items: [
      { href: "/dashboards", label: "Dashboards", icon: LayoutDashboard, ready: true },
      { href: "/ask", label: "Ask", icon: MessagesSquare, ready: true },
    ],
  },
];

function ThemeToggle() {
  const [theme, setTheme] = useState<"light" | "dark">("light");

  useEffect(() => {
    setTheme((document.documentElement.dataset.theme as "light" | "dark") ?? "light");
  }, []);

  const toggle = () => {
    const next = theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    localStorage.setItem("assarium-theme", next);
    setTheme(next);
  };

  const Icon = theme === "dark" ? Sun : Moon;
  return (
    <button
      onClick={toggle}
      aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}
      className="flex size-7 items-center justify-center rounded-[var(--radius-md)] text-[var(--text-subtle)] transition-colors hover:bg-[var(--surface-hover)] hover:text-[var(--text)]"
    >
      <Icon className="size-3.5" aria-hidden />
    </button>
  );
}

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();

  return (
    <div className="flex h-screen overflow-hidden">
      <nav
        className="flex w-[212px] shrink-0 flex-col border-r border-[var(--line)] bg-[var(--nav)]"
        aria-label="Primary"
      >
        <div className="flex h-12 items-center justify-between border-b border-[var(--line)] px-4">
          <Link href="/sources" className="flex items-center gap-2">
            {/* Mark: three stacked rules narrowing upward — the medallion refinement itself. */}
            <svg viewBox="0 0 16 16" width="16" height="16" className="size-4 shrink-0" aria-hidden>
              <rect x="1" y="11.5" width="14" height="2" rx="1" fill="var(--color-bronze, #a0704a)" />
              <rect x="3" y="7" width="10" height="2" rx="1" fill="var(--color-silver, #6f8394)" />
              <rect x="5" y="2.5" width="6" height="2" rx="1" fill="var(--color-gold, #927a38)" />
            </svg>
            <span className="text-[13.5px] font-semibold tracking-[-0.015em]">Assarium</span>
          </Link>
          <ThemeToggle />
        </div>

        <div className="flex-1 overflow-y-auto px-2.5 py-3">
          {NAV.map((group) => (
            <div key={group.section} className="mb-4">
              <p className="mb-1 px-2 text-[10.5px] font-semibold uppercase tracking-[0.07em] text-[var(--text-subtle)]">
                {group.section}
              </p>
              <ul className="space-y-px">
                {group.items.map((item) => {
                  const active = pathname.startsWith(item.href);
                  const content = (
                    <>
                      <item.icon className="size-3.5 shrink-0" aria-hidden />
                      <span className="flex-1 truncate">{item.label}</span>
                      {!item.ready && (
                        <span className="text-[10px] font-normal text-[var(--text-subtle)]">
                          soon
                        </span>
                      )}
                    </>
                  );
                  const className = cn(
                    "flex items-center gap-2 rounded-[var(--radius-md)] px-2 py-1.5 text-[12.5px] font-medium transition-all duration-200",
                    active
                      ? "bg-[var(--accent)]/15 text-[var(--accent)] shadow-[inset_2px_0_0_0_var(--accent)]"
                      : item.ready
                        ? "text-[var(--text-muted)] hover:bg-[var(--surface-hover)] hover:text-[var(--text)] hover:translate-x-0.5"
                        : "cursor-not-allowed text-[var(--text-subtle)]",
                  );
                  return (
                    <li key={item.href}>
                      {item.ready ? (
                        <Link href={item.href} className={className}>
                          {content}
                        </Link>
                      ) : (
                        <span className={className} aria-disabled>
                          {content}
                        </span>
                      )}
                    </li>
                  );
                })}
              </ul>
            </div>
          ))}
        </div>
      </nav>

      <main className="flex min-w-0 flex-1 flex-col overflow-hidden">{children}</main>
    </div>
  );
}
