"use client";

import { useEffect, useState, useCallback } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import { fetchSpecDetail } from "@/lib/api";
import type { SpecDetail } from "@/lib/types";
import StateMachineGraph from "@/components/StateMachineGraph";
import ErrorDisplay from "@/components/ErrorDisplay";
import { Skeleton } from "@/components/ui/skeleton";
import { Card, CardContent } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

function SpecSkeleton() {
  return (
    <div>
      <Skeleton className="h-3.5 w-44 rounded-[2px] mb-1.5" />
      <Skeleton className="h-6 w-52 rounded-[2px] mb-5" />
      <Skeleton className="h-4 w-28 rounded-[2px] mb-2.5" />
      <Skeleton className="h-56 rounded-[2px] mb-6" />
      <Skeleton className="h-4 w-24 rounded-[2px] mb-2.5" />
      <Skeleton className="h-44 rounded-[2px]" />
    </div>
  );
}

export default function SpecViewer() {
  const params = useParams();
  const entity = params.entity as string;
  const [spec, setSpec] = useState<SpecDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await fetchSpecDetail(entity);
      setSpec(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : `Failed to load spec "${entity}"`);
    } finally {
      setLoading(false);
    }
  }, [entity]);

  useEffect(() => {
    load();
  }, [load]);

  if (loading) return <SpecSkeleton />;
  if (error) {
    return (
      <ErrorDisplay
        title={`Spec not found: ${entity}`}
        message={error}
        retry={load}
      />
    );
  }
  if (!spec) {
    return (
      <ErrorDisplay
        title="Spec not found"
        message={`No spec found for entity type "${entity}".`}
      />
    );
  }

  return (
    <div className="animate-fade-in">
      {/* Header */}
      <div className="flex items-center justify-between mb-5">
        <div>
          <div className="flex items-center gap-1.5 text-[12px] text-[var(--color-text-muted)] mb-1">
            <Link href="/" className="hover:text-[var(--color-text-secondary)] transition-colors">Dashboard</Link>
            <span>/</span>
            <span className="text-[var(--color-text-secondary)]">Specs</span>
            <span>/</span>
            <span className="text-[var(--color-text-secondary)]">{spec.entity_type}</span>
          </div>
          <h1 className="text-2xl text-[var(--color-text-primary)] tracking-tight font-serif">{spec.entity_type} Spec</h1>
        </div>
        <Link
          href={`/verify/${spec.entity_type}`}
          className="px-3.5 py-1.5 bg-[var(--color-accent-teal)] hover:bg-[var(--color-accent-teal)] text-[var(--color-bg-primary)] text-[13px] rounded-[2px] transition-colors"
        >
          Run Verification
        </Link>
      </div>

      {/* State Machine Diagram */}
      <div className="mb-6">
        <h2 className="text-base font-semibold text-[var(--color-text-primary)] mb-2.5 tracking-tight">State Machine</h2>
        <StateMachineGraph spec={spec} />
        <div className="flex gap-5 mt-2.5 text-[11px] text-[var(--color-text-muted)]">
          <div className="flex items-center gap-1.5">
            <div className="w-2.5 h-2.5 rounded border border-[var(--color-accent-teal)]/50 bg-[var(--color-accent-teal-dim)]" />
            <span>Initial state</span>
          </div>
          <div className="flex items-center gap-1.5">
            <div className="w-2.5 h-2.5 rounded border border-[var(--color-border)] bg-[var(--color-bg-surface)]" />
            <span>Normal state</span>
          </div>
          <div className="flex items-center gap-1.5">
            <div className="w-2.5 h-2.5 rounded border border-dashed border-[var(--color-text-muted)] bg-[var(--color-bg-surface)]" />
            <span>Terminal state</span>
          </div>
        </div>
      </div>

      {/* Transition Table */}
      <div className="mb-6">
        <h2 className="text-base font-semibold text-[var(--color-text-primary)] mb-2.5 tracking-tight">Transitions</h2>
        <div className="bg-[var(--color-bg-surface)] rounded-[2px] overflow-hidden">
          <Table className="w-full text-[13px]">
            <TableHeader>
              <TableRow className="border-b border-[var(--color-border)] hover:bg-transparent">
                <TableHead className="h-auto px-3.5 py-2.5 text-[11px] text-[var(--color-text-muted)] font-medium uppercase tracking-wider">Action</TableHead>
                <TableHead className="h-auto px-3.5 py-2.5 text-[11px] text-[var(--color-text-muted)] font-medium uppercase tracking-wider">Kind</TableHead>
                <TableHead className="h-auto px-3.5 py-2.5 text-[11px] text-[var(--color-text-muted)] font-medium uppercase tracking-wider">From</TableHead>
                <TableHead className="h-auto px-3.5 py-2.5 text-[11px] text-[var(--color-text-muted)] font-medium uppercase tracking-wider">To</TableHead>
                <TableHead className="h-auto px-3.5 py-2.5 text-[11px] text-[var(--color-text-muted)] font-medium uppercase tracking-wider">Guard</TableHead>
                <TableHead className="h-auto px-3.5 py-2.5 text-[11px] text-[var(--color-text-muted)] font-medium uppercase tracking-wider">Effect</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {spec.actions.map((action, i) => (
                <TableRow
                  key={i}
                  className="border-b border-[var(--color-border)] hover:bg-[var(--color-bg-elevated)] transition-colors"
                >
                  <TableCell className="px-3.5 py-2.5 font-mono text-[var(--color-accent-teal)]">{action.name}</TableCell>
                  <TableCell className="px-3.5 py-2.5">
                    <span
                      className={`text-[10px] font-mono px-1.5 py-0.5 rounded-full ${
                        action.kind === "input"
                          ? "bg-[var(--color-accent-teal-dim)] text-[var(--color-accent-teal)]"
                          : "bg-[var(--color-accent-lime-dim)] text-[var(--color-accent-lime)]"
                      }`}
                    >
                      {action.kind}
                    </span>
                  </TableCell>
                  <TableCell className="px-3.5 py-2.5 font-mono text-[var(--color-text-secondary)]">{action.from.join(", ")}</TableCell>
                  <TableCell className="px-3.5 py-2.5 font-mono text-[var(--color-text-secondary)]">{action.to ?? <span className="text-[var(--color-text-muted)]">--</span>}</TableCell>
                  <TableCell className="px-3.5 py-2.5 font-mono text-[var(--color-accent-pink)]/70 text-[11px]">
                    {action.guards.length > 0 ? action.guards.join("; ") : <span className="text-[var(--color-text-muted)]">--</span>}
                  </TableCell>
                  <TableCell className="px-3.5 py-2.5 font-mono text-[var(--color-text-secondary)] text-[11px]">
                    {action.effects.length > 0 ? action.effects.join("; ") : <span className="text-[var(--color-text-muted)]">--</span>}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      </div>

      {/* Invariants */}
      <div className="mb-6">
        <h2 className="text-base font-semibold text-[var(--color-text-primary)] mb-2.5 tracking-tight">Invariants</h2>
        {spec.invariants.length === 0 ? (
          <Card className="rounded-[2px] border-[var(--color-border)] gap-0"><CardContent className="p-6 text-center">
            <p className="text-[13px] text-[var(--color-text-secondary)]">No invariants defined.</p>
          </CardContent></Card>
        ) : (
          <div className="space-y-1.5">
            {spec.invariants.map((inv, i) => (
              <Card
                key={i}
                className="rounded-[2px] border-[var(--color-border)] gap-0"
              ><CardContent className="p-3">
                <div className="flex items-center gap-2 mb-1.5">
                  <span className="text-[13px] font-semibold text-[var(--color-text-primary)]">{inv.name}</span>
                </div>
                <div className="text-[13px] space-y-0.5">
                  <div className="flex gap-2">
                    <span className="text-[var(--color-text-muted)] w-14 flex-shrink-0 text-[11px]">when</span>
                    <code className="font-mono text-[var(--color-accent-pink)]/70 text-[11px]">{inv.when.length > 0 ? inv.when.join(", ") : "always"}</code>
                  </div>
                  <div className="flex gap-2">
                    <span className="text-[var(--color-text-muted)] w-14 flex-shrink-0 text-[11px]">assert</span>
                    <code className="font-mono text-[var(--color-accent-teal)]/70 text-[11px]">{inv.assertion}</code>
                  </div>
                </div>
              </CardContent></Card>
            ))}
          </div>
        )}
      </div>

      {/* State Variables */}
      <div className="mb-6">
        <h2 className="text-base font-semibold text-[var(--color-text-primary)] mb-2.5 tracking-tight">State Variables</h2>
        {spec.state_variables.length === 0 ? (
          <Card className="rounded-[2px] border-[var(--color-border)] gap-0"><CardContent className="p-6 text-center">
            <p className="text-[13px] text-[var(--color-text-secondary)]">No state variables defined.</p>
          </CardContent></Card>
        ) : (
          <div className="bg-[var(--color-bg-surface)] rounded-[2px] overflow-hidden">
            <Table className="w-full text-[13px]">
              <TableHeader>
                <TableRow className="border-b border-[var(--color-border)] hover:bg-transparent">
                  <TableHead className="h-auto px-3.5 py-2.5 text-[11px] text-[var(--color-text-muted)] font-medium uppercase tracking-wider">Name</TableHead>
                  <TableHead className="h-auto px-3.5 py-2.5 text-[11px] text-[var(--color-text-muted)] font-medium uppercase tracking-wider">Type</TableHead>
                  <TableHead className="h-auto px-3.5 py-2.5 text-[11px] text-[var(--color-text-muted)] font-medium uppercase tracking-wider">Initial Value</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {spec.state_variables.map((v, i) => (
                  <TableRow
                    key={i}
                    className="border-b border-[var(--color-border)] hover:bg-[var(--color-bg-elevated)] transition-colors"
                  >
                    <TableCell className="px-3.5 py-2.5 font-mono text-[var(--color-text-primary)]">{v.name}</TableCell>
                    <TableCell className="px-3.5 py-2.5 font-mono text-[var(--color-accent-lime)] text-[11px]">{v.var_type}</TableCell>
                    <TableCell className="px-3.5 py-2.5 font-mono text-[var(--color-text-secondary)] text-[11px]">{v.initial}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </div>
    </div>
  );
}
