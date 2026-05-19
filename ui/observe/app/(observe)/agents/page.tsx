"use client";

import { useState, useCallback, useMemo } from "react";
import { useRouter } from "next/navigation";
import { fetchAgents } from "@/lib/api";
import { useSSERefresh } from "@/lib/hooks";
import type { AgentsResponse } from "@/lib/types";
import ErrorDisplay from "@/components/ErrorDisplay";
import StatCard from "@/components/StatCard";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

function rateBgClass(rate: number): string {
  if (rate >= 80) return "bg-[var(--color-accent-teal-dim)] text-[var(--color-accent-teal)]";
  if (rate >= 50) return "bg-[var(--color-accent-pink-dim)] text-[var(--color-accent-pink)]";
  return "bg-[var(--color-accent-pink-dim)] text-[var(--color-accent-pink)]";
}

export default function AgentsPage() {
  const router = useRouter();
  const [initialError, setInitialError] = useState<string | null>(null);

  const loadInitial = useCallback(async () => {
    setInitialError(null);
    try {
      await fetchAgents();
    } catch (err) {
      setInitialError(
        err instanceof Error ? err.message : "Failed to load agents",
      );
    }
  }, []);

  const agentsPoll = useSSERefresh<AgentsResponse>({
    fetcher: () => fetchAgents(),
    sseKinds: ["Agents"],
    enabled: !initialError,
  });

  const data = agentsPoll.data;
  const totalDenials = useMemo(() => {
    if (!data) return 0;
    return data.agents.reduce((sum, a) => sum + a.denial_count, 0);
  }, [data]);

  const totalErrors = useMemo(() => {
    if (!data) return 0;
    return data.agents.reduce((sum, a) => sum + a.error_count, 0);
  }, [data]);

  if (initialError) {
    return (
      <ErrorDisplay
        title="Cannot load agents"
        message={initialError}
        retry={loadInitial}
      />
    );
  }

  if (agentsPoll.loading && !data) {
    return (
      <div>
        <Skeleton className="h-6 w-36 mb-1.5" />
        <Skeleton className="h-3.5 w-64 mb-6" />
        <div className="grid grid-cols-4 gap-3 mb-6">
          {[0, 1, 2, 3].map((i) => (
            <Card key={i} className="glass rounded-[2px] border-0 gap-0">
              <CardContent className="p-4">
                <Skeleton className="h-3 w-20 mb-2" />
                <Skeleton className="h-8 w-10" />
              </CardContent>
            </Card>
          ))}
        </div>
      </div>
    );
  }

  return (
    <div className="animate-fade-in">
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl text-[var(--color-text-primary)] tracking-tight font-serif">
            Agents
          </h1>
          <p className="text-sm text-[var(--color-text-muted)] mt-0.5">
            Agent activity, success rates, and authorization denials
          </p>
        </div>
        <div className="flex items-center gap-3">
        </div>
      </div>

      {/* Stats row */}
      <div className="grid grid-cols-4 gap-3 mb-6">
        <StatCard label="Total Agents" value={data?.total ?? 0} />
        <StatCard
          label="Total Denials"
          value={totalDenials}
          color={totalDenials > 0 ? "text-[var(--color-accent-pink)]" : undefined}
        />
        <StatCard
          label="Total Errors"
          value={totalErrors}
          color={totalErrors > 0 ? "text-[var(--color-accent-pink)]" : undefined}
        />
        <StatCard
          label="Active Agents"
          value={
            data?.agents.filter((a) => a.last_active_at !== null).length ?? 0
          }
          color="text-[var(--color-accent-teal)]"
        />
      </div>

      {/* Agent Table */}
      {data && data.agents.length > 0 ? (
        <Card className="glass rounded-[2px] border-0 gap-0 overflow-hidden">
          <Table className="text-[13px]">
            <TableHeader className="sticky top-0 bg-[color-mix(in_srgb,var(--color-bg-surface)_90%,transparent)] backdrop-blur-sm z-10">
              <TableRow className="border-b border-[var(--color-border)] hover:bg-transparent">
                <TableHead className="px-3.5 py-2.5 text-[var(--color-text-muted)] text-xs uppercase tracking-wider h-auto">
                  Agent ID
                </TableHead>
                <TableHead className="px-3.5 py-2.5 text-right text-[var(--color-text-muted)] text-xs uppercase tracking-wider h-auto">
                  Total
                </TableHead>
                <TableHead className="px-3.5 py-2.5 text-right text-[var(--color-text-muted)] text-xs uppercase tracking-wider h-auto">
                  Success
                </TableHead>
                <TableHead className="px-3.5 py-2.5 text-right text-[var(--color-text-muted)] text-xs uppercase tracking-wider h-auto">
                  Errors
                </TableHead>
                <TableHead className="px-3.5 py-2.5 text-right text-[var(--color-text-muted)] text-xs uppercase tracking-wider h-auto">
                  Denials
                </TableHead>
                <TableHead className="px-3.5 py-2.5 text-[var(--color-text-muted)] text-xs uppercase tracking-wider h-auto">
                  Rate
                </TableHead>
                <TableHead className="px-3.5 py-2.5 text-[var(--color-text-muted)] text-xs uppercase tracking-wider h-auto">
                  Entity Types
                </TableHead>
                <TableHead className="px-3.5 py-2.5 text-right text-[var(--color-text-muted)] text-xs uppercase tracking-wider h-auto">
                  Last Active
                </TableHead>
                <TableHead className="px-3.5 py-2.5 h-auto" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.agents.map((agent, i) => {
                const rate = Math.round(agent.success_rate * 100);
                const lastActive = agent.last_active_at
                  ? new Date(agent.last_active_at).toLocaleString()
                  : "--";
                return (
                  <TableRow
                    key={agent.agent_id}
                    onClick={() => router.push(`/agents/${encodeURIComponent(agent.agent_id)}`)}
                    className={`border-b border-[var(--color-border)] cursor-pointer ${i % 2 === 1 ? "bg-[var(--color-bg-elevated)]" : ""}`}
                  >
                    <TableCell className="px-3.5 py-2.5">
                      <span className="font-mono text-[var(--color-text-primary)]">
                        {agent.agent_id}
                      </span>
                    </TableCell>
                    <TableCell className="px-3.5 py-2.5 text-right font-mono text-[var(--color-text-secondary)]">
                      {agent.total_actions}
                    </TableCell>
                    <TableCell className="px-3.5 py-2.5 text-right font-mono text-[var(--color-accent-teal)]">
                      {agent.success_count}
                    </TableCell>
                    <TableCell className="px-3.5 py-2.5 text-right font-mono text-[var(--color-accent-pink)]">
                      {agent.error_count}
                    </TableCell>
                    <TableCell className="px-3.5 py-2.5 text-right font-mono text-[var(--color-accent-pink)]">
                      {agent.denial_count}
                    </TableCell>
                    <TableCell className="px-3.5 py-2.5">
                      <span className={`text-xs font-mono px-2 py-0.5 rounded-full ${rateBgClass(rate)}`}>
                        {rate}%
                      </span>
                    </TableCell>
                    <TableCell className="px-3.5 py-2.5">
                      <div className="flex flex-wrap gap-1">
                        {agent.entity_types.map((et) => (
                          <span
                            key={et}
                            className="text-[10px] font-mono bg-[var(--color-bg-elevated)] text-[var(--color-text-secondary)] px-1.5 py-0.5 rounded-sm"
                          >
                            {et}
                          </span>
                        ))}
                      </div>
                    </TableCell>
                    <TableCell className="px-3.5 py-2.5 text-right font-mono text-[var(--color-text-muted)] text-[11px]">
                      {lastActive}
                    </TableCell>
                    <TableCell className="px-3.5 py-2.5 text-right">
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        onClick={(e) => {
                          e.stopPropagation();
                          router.push(`/agents/${encodeURIComponent(agent.agent_id)}?tab=policies`);
                        }}
                        className="rounded-[2px] text-[10px] bg-[var(--color-accent-teal-dim)] text-[var(--color-accent-teal)]"
                      >
                        Permissions
                      </Button>
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </Card>
      ) : (
        <Card className="glass rounded-[2px] border-0 gap-0">
          <CardContent className="p-6 text-center">
            <p className="text-sm text-[var(--color-text-secondary)]">
              No agent activity recorded yet.
            </p>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
