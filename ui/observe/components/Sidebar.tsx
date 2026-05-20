"use client";

import { useState, useEffect } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  LayoutGrid,
  Zap,
  ShieldCheck,
  Lock,
  Package,
  Activity,
  Dna,
  Users,
  Lightbulb,
  Download,
  Network,
} from "lucide-react";
import { useConnection } from "@/lib/connection";
import { useDecisionNotifier } from "@/lib/decision-notifier";
import { fetchUnmetIntents } from "@/lib/api";
import UserMenu from "@/components/UserMenu";
import ThemeToggle from "@/components/ThemeToggle";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

const ICON_MAP: Record<string, React.ComponentType<{ className?: string }>> = {
  grid: LayoutGrid,
  workflow: Zap,
  shield: ShieldCheck,
  lock: Lock,
  box: Package,
  activity: Activity,
  dna: Dna,
  users: Users,
  lightbulb: Lightbulb,
  package: Download,
  network: Network,
};

const navItems = [
  { href: "/dashboard", label: "Dashboard", icon: "grid" },
  { href: "/workflows", label: "Workflows", icon: "workflow" },
  { href: "/activity", label: "Activity", icon: "activity" },
  { href: "/decisions", label: "Decisions", icon: "shield" },
  { href: "/policies", label: "Policies", icon: "lock" },
  { href: "/agents", label: "Agents", icon: "users" },
  { href: "/evolution", label: "Evolution", icon: "dna" },
  { href: "/feature-requests", label: "Feature Requests", icon: "lightbulb" },
  { href: "/integrations", label: "Integrations", icon: "box" },
  { href: "/os-apps", label: "Apps", icon: "package" },
];

export default function Sidebar() {
  const pathname = usePathname();
  const { connected, checking } = useConnection();
  const { pendingCount } = useDecisionNotifier();
  const [unmetCount, setUnmetCount] = useState(0);

  useEffect(() => {
    let mounted = true;
    const poll = async () => {
      try {
        const data = await fetchUnmetIntents();
        if (mounted) setUnmetCount(data.open_count);
      } catch { /* ignore */ }
    };
    poll();
    const interval = setInterval(poll, 30000);
    return () => { mounted = false; clearInterval(interval); };
  }, []);

  const isActive = (href: string) => {
    if (href === "/dashboard") return pathname === "/dashboard";
    return pathname.startsWith(href);
  };

  return (
    <aside className="w-52 bg-[var(--color-bg-primary)]/80 backdrop-blur-xl border-r border-[var(--color-border)] flex flex-col h-screen">
      <div className="px-4 py-3.5">
        <Link href="/dashboard" className="flex items-center gap-2.5">
          <div>
            <div className="text-[15px] font-bold text-[var(--color-text-primary)] tracking-tight font-display">Temper</div>
            <div className="text-[10px] text-[var(--color-text-muted)] tracking-wide uppercase">Observe</div>
          </div>
        </Link>
      </div>

      <nav className="flex-1 px-2 py-2 space-y-0.5 overflow-y-auto" aria-label="Main navigation">
        {navItems.map((item) => {
          const Icon = ICON_MAP[item.icon];
          const active = isActive(item.href);
          const hasPendingDecisions = item.label === "Decisions" && pendingCount > 0;
          const hasUnmet = item.label === "Evolution" && unmetCount > 0;

          return (
            <Link
              key={item.href}
              href={item.href}
              className={cn(
                "flex items-center gap-2.5 px-2.5 py-1.5 rounded-[2px] text-[13px] font-display transition-colors",
                active
                  ? "text-[var(--color-text-primary)] bg-[var(--color-bg-elevated)]"
                  : hasPendingDecisions
                    ? "text-[var(--color-accent-pink)] bg-[var(--color-accent-pink-dim)] hover:bg-[var(--color-accent-pink-dim)]"
                    : "text-[var(--color-text-secondary)] hover:text-[var(--color-text-primary)] hover:bg-[var(--color-bg-elevated)]"
              )}
            >
              {Icon && <Icon className="w-4 h-4 flex-shrink-0" />}
              {item.label}
              {hasPendingDecisions && (
                <Badge
                  variant="outline"
                  className="ml-auto text-[10px] font-mono bg-[var(--color-accent-pink-dim)] text-[var(--color-accent-pink)] border-transparent min-w-[20px] text-center"
                  aria-label={`${pendingCount} pending decisions`}
                >
                  {pendingCount > 99 ? "99+" : pendingCount}
                </Badge>
              )}
              {hasUnmet && (
                <Badge
                  variant="outline"
                  className="ml-auto text-[10px] font-mono bg-[var(--color-accent-pink-dim)] text-[var(--color-accent-pink)] border-transparent min-w-[20px] text-center"
                  aria-label={`${unmetCount} unmet intents`}
                >
                  {unmetCount > 99 ? "99+" : unmetCount}
                </Badge>
              )}
            </Link>
          );
        })}
      </nav>

      <div className="border-t border-[var(--color-border)]">
        <UserMenu />
      </div>

      <div className="px-4 py-3 border-t border-[var(--color-border)] flex items-center justify-between">
        <div className="text-[10px] text-[var(--color-text-muted)] font-mono">TEMPER v0.1.0</div>
        <div className="flex items-center gap-2">
          <ThemeToggle />
          <div
            className="flex items-center gap-1.5"
            aria-label={checking ? "Checking connection" : connected ? "Connected" : "Disconnected"}
          >
            <div className={cn(
              "w-1.5 h-1.5 rounded-full",
              checking ? "bg-[var(--color-text-muted)]" : connected ? "bg-[var(--color-accent-teal)]" : "bg-[var(--color-accent-pink)]"
            )} />
            <span className={cn(
              "text-[10px] font-mono",
              checking ? "text-[var(--color-text-muted)]" : connected ? "text-[var(--color-text-muted)]" : "text-[var(--color-accent-pink)]"
            )}>
              {checking ? "..." : connected ? "" : "offline"}
            </span>
          </div>
        </div>
      </div>
    </aside>
  );
}
