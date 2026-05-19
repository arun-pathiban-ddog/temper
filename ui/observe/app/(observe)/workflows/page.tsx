"use client";

import { useEffect, useState, useMemo } from "react";
import { fetchWorkflows, deleteTenant } from "@/lib/api";
import { useSSERefresh } from "@/lib/hooks";
import type { WorkflowsResponse, AppWorkflow } from "@/lib/types";
import ErrorDisplay from "@/components/ErrorDisplay";
import Link from "next/link";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Card, CardContent } from "@/components/ui/card";
import { Trash2 } from "lucide-react";

function StatusIndicator({ status }: { status: string }) {
  const config: Record<string, { color: string; label: string; animate?: boolean }> = {
    loading: { color: "bg-[var(--color-accent-teal)]", label: "Loading", animate: true },
    verifying: { color: "bg-[var(--color-accent-pink)]", label: "Verifying", animate: true },
    completed: { color: "bg-[var(--color-accent-teal)]", label: "Verified" },
    failed: { color: "bg-[var(--color-accent-pink)]", label: "Failed" },
  };
  const c = config[status] ?? config.loading;
  return (
    <div className="flex items-center gap-1.5">
      <div className={`w-1.5 h-1.5 rounded-full ${c.color} ${c.animate ? "animate-pulse" : ""}`} />
      <span className="text-[11px] text-[var(--color-text-secondary)]">{c.label}</span>
    </div>
  );
}

function AppCard({ workflow, onDelete }: { workflow: AppWorkflow; onDelete: (tenant: string) => void }) {
  const totalSteps = workflow.entities.length * 7;
  const completedSteps = workflow.entities.reduce(
    (acc, e) => acc + e.steps.filter((s) => s.status === "completed" || s.status === "failed").length,
    0,
  );
  const progressPct = totalSteps > 0 ? Math.round((completedSteps / totalSteps) * 100) : 0;
  const allPassed = workflow.entities.every((e) =>
    e.steps.every((s) => s.status !== "failed" && s.passed !== false),
  );

  return (
    <Card className="rounded-[2px] border-[var(--color-border)] bg-[var(--color-bg-surface)] hover:bg-[var(--color-bg-elevated)] gap-0 group overflow-hidden transition-colors">
      <Link href={`/workflows/${workflow.tenant}`} className="block">
        <CardContent className="p-5">
          <div className="flex items-center justify-between mb-2.5">
            <h3 className="text-base font-semibold text-[var(--color-text-primary)] tracking-tight">{workflow.tenant}</h3>
            <div className="flex items-center gap-2">
              <StatusIndicator status={workflow.status} />
              {workflow.tenant !== "temper-system" && (
                <Button variant="ghost" size="icon"
                  onClick={(e) => { e.preventDefault(); e.stopPropagation(); onDelete(workflow.tenant); }}
                  title={`Delete tenant ${workflow.tenant}`}
                  className="opacity-0 group-hover:opacity-100 transition-opacity h-6 w-6 text-[var(--color-text-muted)] hover:text-[var(--color-accent-pink)]">
                  <Trash2 className="w-3.5 h-3.5" />
                </Button>
              )}
            </div>
          </div>

          {/* Entity count */}
          <div className="text-[13px] text-[var(--color-text-secondary)] mb-2.5">
            {workflow.entities.length} {workflow.entities.length === 1 ? "entity" : "entities"}
            {workflow.runtime_events_count > 0 && (
              <span className="ml-2.5 text-[var(--color-text-muted)]">
                {workflow.runtime_events_count} runtime events
              </span>
            )}
          </div>

          {/* Progress bar */}
          <div className="h-1 bg-[var(--color-bg-elevated)] rounded-full overflow-hidden mb-2.5">
            <div
              className={`h-full rounded-full transition-all duration-500 ${
                workflow.status === "failed" || !allPassed
                  ? "bg-[var(--color-accent-pink)]"
                  : workflow.status === "completed"
                  ? "bg-[var(--color-accent-teal)]"
                  : "bg-[var(--color-accent-teal)]"
              }`}
              style={{ width: `${progressPct}%` }}
            />
          </div>

          {/* Entity dots */}
          <div className="flex flex-wrap gap-2">
            {workflow.entities.map((entity) => {
              const entityDone = entity.steps.every(
                (s) => s.status === "completed" || s.status === "failed",
              );
              const entityFailed = entity.steps.some(
                (s) => s.status === "failed" || s.passed === false,
              );
              const entityRunning = entity.steps.some((s) => s.status === "running");
              const dotColor = entityFailed
                ? "bg-[var(--color-accent-pink)]"
                : entityDone
                ? "bg-[var(--color-accent-teal)]"
                : entityRunning
                ? "bg-[var(--color-accent-pink)] animate-pulse"
                : "bg-[var(--color-text-muted)]";

              return (
                <div key={entity.entity_type} className="flex items-center gap-1.5">
                  <div className={`w-1.5 h-1.5 rounded-full ${dotColor}`} />
                  <span className="text-[11px] text-[var(--color-text-secondary)] font-mono">{entity.entity_type}</span>
                </div>
              );
            })}
          </div>
        </CardContent>
      </Link>
    </Card>
  );
}

function WorkflowsSkeleton() {
  return (
    <div className="space-y-6">
      <div className="space-y-1.5">
        <Skeleton className="h-6 w-36" />
        <Skeleton className="h-3.5 w-72" />
      </div>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        {[0, 1].map((i) => (
          <Skeleton key={i} className="rounded-[2px] h-36" />
        ))}
      </div>
    </div>
  );
}

export default function WorkflowsPage() {
  const [initialLoading, setInitialLoading] = useState(true);
  const [initialError, setInitialError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);

  const poll = useSSERefresh<WorkflowsResponse>({
    fetcher: fetchWorkflows,
    sseKinds: ["Specs"],
    enabled: !initialLoading && !initialError,
  });
  const workflows = useMemo(() => poll.data?.workflows ?? [], [poll.data]);

  useEffect(() => {
    fetchWorkflows()
      .then(() => setInitialLoading(false))
      .catch((err) => {
        setInitialError(err instanceof Error ? err.message : "Failed to load workflows");
        setInitialLoading(false);
      });
  }, []);

  const handleDelete = async (tenant: string) => {
    if (!confirm(`Delete tenant "${tenant}"? This removes all specs and data.`)) return;
    setDeleting(tenant);
    try {
      await deleteTenant(tenant);
      poll.refresh();
    } catch (err) {
      alert(err instanceof Error ? err.message : "Failed to delete tenant");
    } finally {
      setDeleting(null);
    }
  };

  if (initialLoading) return <WorkflowsSkeleton />;
  if (initialError)
    return (
      <ErrorDisplay
        title="Cannot load workflows"
        message={initialError}
        retry={() => window.location.reload()}
      />
    );

  return (
    <div className="animate-fade-in">
      <div className="mb-6">
        <h1 className="text-2xl text-[var(--color-text-primary)] tracking-tight font-serif">Workflows</h1>
        <p className="text-sm text-[var(--color-text-muted)] mt-0.5">
          App deployment workflows — spec loading, verification cascade, and runtime
        </p>
      </div>

      {workflows.length === 0 ? (
        <div className="flex items-center justify-center min-h-[256px]">
          <div className="text-center max-w-md">
            <div className="inline-flex items-center justify-center w-10 h-10 rounded-full bg-[var(--color-bg-elevated)] mb-4">
              <svg className="w-5 h-5 text-[var(--color-text-muted)]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M13 10V3L4 14h7v7l9-11h-7z" />
              </svg>
            </div>
            <h3 className="text-base font-semibold text-[var(--color-text-primary)] mb-1">No app workflows</h3>
            <p className="text-sm text-[var(--color-text-secondary)]">
              Start the server with{" "}
              <code className="font-mono text-[11px] bg-[var(--color-bg-elevated)] px-1.5 py-0.5 rounded">
                temper serve --app name=specs-dir
              </code>
            </p>
          </div>
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          {workflows.filter((w) => w.tenant !== deleting).map((w) => (
            <AppCard key={w.tenant} workflow={w} onDelete={handleDelete} />
          ))}
        </div>
      )}
    </div>
  );
}
