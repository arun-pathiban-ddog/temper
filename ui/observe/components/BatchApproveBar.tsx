"use client";

import { useState, useCallback } from "react";
import type { PendingDecision, PolicyScopeMatrix } from "@/lib/types";
import type { PolicyBuilderContext } from "./PolicyBuilder";
import PolicyBuilder from "./PolicyBuilder";
import { commonContext } from "@/lib/decision-grouping";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";

interface BatchApproveBarProps {
  selectedDecisions: PendingDecision[];
  onApprove: (ids: string[], matrix: PolicyScopeMatrix) => Promise<{ succeeded: number; failed: number }>;
  onClear: () => void;
}

export default function BatchApproveBar({ selectedDecisions, onApprove, onClear }: BatchApproveBarProps) {
  const [showBuilder, setShowBuilder] = useState(false);
  const [approving, setApproving] = useState(false);
  const [result, setResult] = useState<{ succeeded: number; failed: number } | null>(null);

  const ctx: PolicyBuilderContext = commonContext(selectedDecisions);
  const ids = selectedDecisions.map((d) => d.id);

  const handleApprove = useCallback(
    async (matrix: PolicyScopeMatrix) => {
      setApproving(true);
      setResult(null);
      try {
        const res = await onApprove(ids, matrix);
        setResult(res);
        if (res.failed === 0) {
          setTimeout(() => { setShowBuilder(false); setResult(null); }, 1500);
        }
      } finally {
        setApproving(false);
      }
    },
    [ids, onApprove],
  );

  if (selectedDecisions.length === 0) return null;

  return (
    <div className="fixed bottom-0 left-0 right-0 z-50 border-t border-[var(--color-border)] bg-[color-mix(in_srgb,var(--color-bg-primary)_95%,transparent)] backdrop-blur-md">
      <div className="max-w-5xl mx-auto px-6 py-3">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-2 h-2 rounded-full bg-[var(--color-accent-teal)] animate-pulse" />
            <span className="text-sm text-[var(--color-text-primary)]">
              <Badge variant="outline" className="font-mono mr-1.5 bg-[var(--color-accent-teal-dim)] text-[var(--color-accent-teal)] border-transparent">
                {selectedDecisions.length}
              </Badge>
              {selectedDecisions.length === 1 ? "decision" : "decisions"} selected
            </span>
          </div>
          <div className="flex items-center gap-2">
            {result && (
              <span className={`text-xs font-mono ${result.failed > 0 ? "text-[var(--color-accent-pink)]" : "text-[var(--color-accent-teal)]"}`}>
                {result.succeeded} approved{result.failed > 0 ? `, ${result.failed} failed` : ""}
              </span>
            )}
            <Button
              size="sm"
              variant="outline"
              onClick={() => setShowBuilder(!showBuilder)}
              className="rounded-[2px] text-xs bg-[var(--color-accent-teal-dim)] text-[var(--color-accent-teal)] border-transparent hover:bg-[var(--color-accent-teal-dim)]"
            >
              {showBuilder ? "Hide" : "Approve Selected"}
            </Button>
            <Button size="sm" variant="secondary" onClick={onClear} className="rounded-[2px] text-xs">
              Clear
            </Button>
          </div>
        </div>

        {showBuilder && (
          <>
            <Separator className="my-3 bg-[var(--color-border)]" />
            <PolicyBuilder
              context={ctx}
              onApprove={handleApprove}
              onCancel={() => setShowBuilder(false)}
              disabled={approving}
            />
          </>
        )}
      </div>
    </div>
  );
}
