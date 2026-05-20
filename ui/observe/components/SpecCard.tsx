"use client";

import { useRef, useState, useEffect } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import type { SpecSummary } from "@/lib/types";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

interface SpecCardProps {
  spec: SpecSummary;
}

function VerificationBadge({ spec }: { spec: SpecSummary }) {
  const status = spec.verification_status;
  const levelInfo =
    spec.levels_passed != null && spec.levels_total != null
      ? `${spec.levels_passed}/${spec.levels_total}`
      : null;

  const config: Record<string, { className: string; label: string; pulse?: boolean }> = {
    pending: { className: "bg-[var(--color-accent-lime-dim)] text-[var(--color-text-secondary)] border-transparent", label: "Pending" },
    running: { className: "bg-[var(--color-accent-pink-dim)] text-[var(--color-accent-pink)] border-transparent", label: "Verifying...", pulse: true },
    passed: { className: "bg-[var(--color-accent-teal-dim)] text-[var(--color-accent-teal)] border-transparent", label: "Verified" },
    failed: { className: "bg-[var(--color-accent-pink-dim)] text-[var(--color-accent-pink)] border-transparent", label: "Failed" },
    partial: { className: "bg-[var(--color-accent-pink-dim)] text-[var(--color-accent-pink)] border-transparent", label: levelInfo ? `Partial (${levelInfo})` : "Partial" },
  };

  const c = config[status] ?? config.pending;

  return (
    <Badge
      variant="outline"
      className={cn("font-mono text-[10px]", c.className, c.pulse && "animate-pulse")}
    >
      {c.label}
    </Badge>
  );
}

export default function SpecCard({ spec }: SpecCardProps) {
  const router = useRouter();
  const prevVerifyRef = useRef(spec.verification_status);
  const [cardFlash, setCardFlash] = useState("");

  useEffect(() => {
    if (prevVerifyRef.current !== spec.verification_status) {
      setCardFlash(
        spec.verification_status === "passed" ? "animate-flash-teal" :
        spec.verification_status === "failed" ? "animate-flash-pink" : ""
      );
      prevVerifyRef.current = spec.verification_status;
    }
  }, [spec.verification_status]);

  return (
    <Link href={`/specs/${spec.entity_type}`}>
      <Card
        className={cn(
          "rounded-[2px] border-0 bg-[var(--color-bg-surface)] hover:bg-[var(--color-bg-elevated)] transition-colors cursor-pointer group",
          cardFlash
        )}
        onAnimationEnd={() => setCardFlash("")}
      >
        <CardHeader className="p-5 pb-2.5">
          <div className="flex items-start justify-between">
            <h3 className="text-base font-semibold text-[var(--color-text-primary)] tracking-tight truncate min-w-0" title={spec.entity_type}>
              {spec.entity_type}
            </h3>
            <div className="flex gap-1.5 flex-shrink-0 ml-2">
              <VerificationBadge spec={spec} />
              <Badge variant="outline" className="font-mono text-[10px] bg-[var(--color-accent-teal-dim)] text-[var(--color-accent-teal)] border-transparent">
                IOA
              </Badge>
            </div>
          </div>
        </CardHeader>

        <CardContent className="px-5 pb-5">
          <div className="space-y-1.5">
            <div className="flex items-center justify-between text-sm">
              <span className="text-[var(--color-text-muted)]">States</span>
              <span className="font-mono text-[var(--color-text-secondary)]">{spec.states.length}</span>
            </div>
            <div className="flex items-center justify-between text-sm">
              <span className="text-[var(--color-text-muted)]">Actions</span>
              <span className="font-mono text-[var(--color-text-secondary)]">{spec.actions.length}</span>
            </div>
            <div className="flex items-center justify-between text-sm">
              <span className="text-[var(--color-text-muted)]">Initial</span>
              <span className="font-mono text-[var(--color-accent-lime)]">{spec.initial_state}</span>
            </div>
          </div>

          <div className="mt-3 flex flex-wrap gap-1">
            {spec.states.map((state) => (
              <Badge
                key={state}
                variant="outline"
                className={cn(
                  "font-mono text-[10px] border-transparent",
                  state === spec.initial_state
                    ? "bg-[var(--color-accent-lime-dim)] text-[var(--color-accent-lime)]"
                    : "bg-[var(--color-bg-elevated)] text-[var(--color-text-secondary)]"
                )}
              >
                {state}
              </Badge>
            ))}
          </div>

          <div className="mt-3">
            <Button
              variant="link"
              size="sm"
              className="text-[11px] text-[var(--color-accent-teal)] p-0 h-auto"
              onClick={(e) => {
                e.preventDefault();
                e.stopPropagation();
                router.push(`/verify/${spec.entity_type}`);
              }}
            >
              Verify
            </Button>
          </div>
        </CardContent>
      </Card>
    </Link>
  );
}
