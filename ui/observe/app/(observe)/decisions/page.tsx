"use client";

import { useEffect, useState, useCallback, useMemo } from "react";
import {
  fetchDecisions,
  fetchAllDecisions,
  approveDecision,
  denyDecision,
  subscribePendingDecisions,
  subscribeAllPendingDecisions,
  fetchSpecs,
} from "@/lib/api";
import { useSSERefresh } from "@/lib/hooks";
import type {
  DecisionsResponse,
  PendingDecision,
  PolicyScopeMatrix,
  SpecSummary,
} from "@/lib/types";
import ErrorDisplay from "@/components/ErrorDisplay";
import StatCard from "@/components/StatCard";
import PolicyBuilder from "@/components/PolicyBuilder";
import DecisionGroup from "@/components/DecisionGroup";
import BatchApproveBar from "@/components/BatchApproveBar";
import {
  redactSensitiveFields,
  groupByDate,
} from "@/lib/utils";
import { groupDecisions, type GroupingStrategy } from "@/lib/decision-grouping";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Alert, AlertDescription } from "@/components/ui/alert";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Card, CardContent } from "@/components/ui/card";

const ALL_TENANTS = "__all__";

/** Redact inline secrets in denial reason strings (e.g. "token=sk-abc123"). */
function redactDenialReason(reason: string): string {
  return reason.replace(
    /\b(authorization|api_key|apikey|token|secret|password|cookie|credential|bearer|jwt|session_token|access_token|refresh_token|private_key)\s*[=:]\s*\S+/gi,
    (match) => {
      const sep = match.includes("=") ? "=" : ":";
      const key = match.split(/[=:]/)[0].trim();
      return `${key}${sep}[redacted]`;
    },
  );
}

function DecisionCard({
  decision,
  onApprove,
  onDeny,
  acting,
  showTenant,
}: {
  decision: PendingDecision;
  onApprove: (id: string, matrix: PolicyScopeMatrix, tenant: string) => void;
  onDeny: (id: string, tenant: string) => void;
  acting: boolean;
  showTenant?: boolean;
}) {
  const ts = new Date(decision.created_at);
  const timeStr = ts.toLocaleString();

  const redactedAttrs = redactSensitiveFields(decision.resource_attrs);

  return (
    <Card className="glass rounded-[2px] border-0 gap-0 animate-fade-in">
      <CardContent className="p-4">
        <div className="flex items-start justify-between mb-3">
          <div className="flex items-center gap-2">
            <div className="w-2 h-2 rounded-full bg-[var(--color-accent-pink)] animate-pulse" />
            <span className="text-sm font-mono text-[var(--color-text-primary)] truncate max-w-[200px]" title={decision.agent_id}>
              {decision.agent_id}
            </span>
            {showTenant && (
              <span className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-[var(--color-bg-elevated)] text-[var(--color-text-secondary)]">
                {decision.tenant}
              </span>
            )}
          </div>
          <span className="text-[11px] text-[var(--color-text-muted)] font-mono">{timeStr}</span>
        </div>

        <div className="space-y-1.5 mb-3">
          <div className="flex items-center gap-2">
            <span className="text-[10px] text-[var(--color-text-muted)] uppercase tracking-wider w-16">
              Action
            </span>
            <span className="text-[13px] font-mono text-[var(--color-accent-teal)]">
              {decision.action}
            </span>
          </div>
          <div className="flex items-center gap-2">
            <span className="text-[10px] text-[var(--color-text-muted)] uppercase tracking-wider w-16">
              Resource
            </span>
            <span className="text-[13px] font-mono text-[var(--color-text-secondary)] truncate max-w-[280px] inline-block" title={`${decision.resource_type}::${decision.resource_id}`}>
              {decision.resource_type}::{decision.resource_id}
            </span>
          </div>
          <div className="flex items-center gap-2">
            <span className="text-[10px] text-[var(--color-text-muted)] uppercase tracking-wider w-16">
              Reason
            </span>
            <span className="text-[13px] text-[var(--color-accent-pink)]">
              {redactDenialReason(decision.denial_reason)}
            </span>
          </div>
          {decision.module_name && (
            <div className="flex items-center gap-2">
              <span className="text-[10px] text-[var(--color-text-muted)] uppercase tracking-wider w-16">
                Module
              </span>
              <span className="text-[13px] font-mono text-[var(--color-text-secondary)]">
                {decision.module_name}
              </span>
            </div>
          )}
          {decision.resource_attrs &&
            Object.keys(decision.resource_attrs).length > 0 && (
              <div className="flex items-start gap-2">
                <span className="text-[10px] text-[var(--color-text-muted)] uppercase tracking-wider w-16 pt-0.5">
                  Attrs
                </span>
                <pre className="text-[11px] font-mono text-[var(--color-text-secondary)] overflow-x-auto whitespace-pre-wrap">
                  {JSON.stringify(redactedAttrs, null, 2)}
                </pre>
              </div>
            )}
        </div>

        {/* Policy Builder replaces old scope dropdown */}
        <div className="pt-2 border-t border-[var(--color-border)]">
          <PolicyBuilder
            decision={decision}
            onApprove={(matrix) => onApprove(decision.id, matrix, decision.tenant)}
            onDeny={() => onDeny(decision.id, decision.tenant)}
            disabled={acting}
          />
        </div>
      </CardContent>
    </Card>
  );
}

function HistoryRow({
  decision,
  showTenant,
  even,
}: {
  decision: PendingDecision;
  showTenant: boolean;
  even: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const decidedTs = decision.decided_at
    ? new Date(decision.decided_at).toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      })
    : "--";

  return (
    <>
      <TableRow
        className={`cursor-pointer border-b border-[var(--color-border)] ${even ? "bg-[var(--color-bg-elevated)]" : ""}`}
        onClick={() =>
          decision.generated_policy && setExpanded((prev) => !prev)
        }
      >
        {showTenant && (
          <TableCell className="px-3 py-2 font-mono text-[var(--color-text-secondary)] text-[11px] whitespace-nowrap">
            {decision.tenant}
          </TableCell>
        )}
        <TableCell className="px-3 py-2 font-mono text-[var(--color-text-secondary)] max-w-[140px] truncate" title={decision.agent_id}>
          {decision.agent_id}
        </TableCell>
        <TableCell className="px-3 py-2 font-mono text-[var(--color-accent-teal)] max-w-[100px] truncate" title={decision.action}>
          {decision.action}
        </TableCell>
        <TableCell className="px-3 py-2 font-mono text-[var(--color-text-secondary)] max-w-[160px] truncate" title={`${decision.resource_type}::${decision.resource_id}`}>
          {decision.resource_type}::{decision.resource_id}
        </TableCell>
        <TableCell className="px-3 py-2 whitespace-nowrap">
          <span
            className={`text-[11px] font-mono px-1.5 py-0.5 rounded-full ${
              decision.status === "approved"
                ? "bg-[var(--color-accent-teal-dim)] text-[var(--color-accent-teal)]"
                : decision.status === "denied"
                  ? "bg-[var(--color-accent-pink-dim)] text-[var(--color-accent-pink)]"
                  : "bg-[var(--color-accent-pink-dim)] text-[var(--color-accent-pink)]"
            }`}
          >
            {decision.status}
          </span>
        </TableCell>
        <TableCell className="px-3 py-2 font-mono text-[var(--color-text-secondary)] text-[11px] max-w-[180px] truncate" title={decision.approved_scope ? `${decision.approved_scope.principal} / ${decision.approved_scope.action} / ${decision.approved_scope.resource}` : undefined}>
          {decision.approved_scope ? `${decision.approved_scope.principal} / ${decision.approved_scope.action} / ${decision.approved_scope.resource}` : "--"}
        </TableCell>
        <TableCell className="px-3 py-2 text-right font-mono text-[var(--color-text-muted)] text-[11px] whitespace-nowrap">
          {decidedTs}
          {decision.generated_policy && (
            <span className="ml-1 text-[var(--color-text-muted)]">
              {expanded ? "\u25B4" : "\u25BE"}
            </span>
          )}
        </TableCell>
      </TableRow>
      {expanded && decision.generated_policy && (
        <TableRow className="border-b border-[var(--color-border)]">
          <TableCell
            colSpan={showTenant ? 7 : 6}
            className="px-3.5 py-2.5"
          >
            <pre className="p-2.5 bg-black/30 rounded text-[11px] font-mono text-[var(--color-text-secondary)] overflow-x-auto whitespace-pre-wrap border border-[var(--color-border)]">
              {decision.generated_policy}
            </pre>
          </TableCell>
        </TableRow>
      )}
    </>
  );
}

function exportDecisions(decisions: PendingDecision[]) {
  const dateStr = new Date().toISOString().slice(0, 10);
  const blob = new Blob([JSON.stringify(decisions, null, 2)], {
    type: "application/json",
  });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `temper-decisions-${dateStr}.json`;
  a.click();
  URL.revokeObjectURL(url);
}

export default function DecisionsPage() {
  const [initialLoading, setInitialLoading] = useState(true);
  const [initialError, setInitialError] = useState<string | null>(null);
  const [tenant, setTenant] = useState<string>(ALL_TENANTS);
  const [tenants, setTenants] = useState<string[]>([]);
  const [statusFilter, setStatusFilter] = useState<string>("all");
  const [actingIds, setActingIds] = useState<Set<string>>(new Set());
  const [actionError, setActionError] = useState<string | null>(null);
  const [liveDecisions, setLiveDecisions] = useState<PendingDecision[]>([]);
  const [batchMode, setBatchMode] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [groupingStrategy, setGroupingStrategy] = useState<GroupingStrategy>("action_resource");

  const loadInitial = useCallback(async () => {
    setInitialLoading(true);
    setInitialError(null);
    try {
      const specs = await fetchSpecs();
      const tenantSet = new Set<string>();
      for (const s of specs) {
        if (s.tenant && s.tenant !== "temper-system") tenantSet.add(s.tenant);
      }
      setTenants(Array.from(tenantSet).sort());
    } catch (err) {
      setInitialError(
        err instanceof Error ? err.message : "Failed to load decisions",
      );
    } finally {
      setInitialLoading(false);
    }
  }, []);

  useEffect(() => {
    loadInitial();
  }, [loadInitial]);

  const decisionsPoll = useSSERefresh<DecisionsResponse>({
    fetcher: () =>
      tenant === ALL_TENANTS
        ? fetchAllDecisions(
            statusFilter !== "all" ? { status: statusFilter } : undefined,
          )
        : fetchDecisions(
            tenant,
            statusFilter !== "all" ? { status: statusFilter } : undefined,
          ),
    sseKinds: ["Decisions"],
    enabled: !initialLoading && !initialError,
  });

  const data = decisionsPoll.data;
  // SSE for live pending decisions (best-effort — may fail without admin headers)
  useEffect(() => {
    if (initialLoading || initialError) return;
    const cleanup =
      tenant === ALL_TENANTS
        ? subscribeAllPendingDecisions((decision) => {
            setLiveDecisions((prev) => [...prev.slice(-49), decision]);
            decisionsPoll.refresh().then(() => setLiveDecisions([]));
          })
        : subscribePendingDecisions(tenant, (decision) => {
            setLiveDecisions((prev) => [...prev.slice(-49), decision]);
            decisionsPoll.refresh().then(() => setLiveDecisions([]));
          });
    return cleanup;
  }, [initialLoading, initialError, tenant]); // eslint-disable-line react-hooks/exhaustive-deps

  const handleApprove = useCallback(
    async (id: string, matrix: PolicyScopeMatrix, decisionTenant: string) => {
      setActingIds((prev) => new Set(prev).add(id));
      setActionError(null);
      try {
        await approveDecision(decisionTenant, id, matrix);
        await decisionsPoll.refresh();
      } catch (err) {
        const msg = err instanceof Error ? err.message : "Failed to approve decision";
        setActionError(msg);
      } finally {
        setActingIds((prev) => {
          const next = new Set(prev);
          next.delete(id);
          return next;
        });
      }
    },
    [decisionsPoll],
  );

  const handleDeny = useCallback(
    async (id: string, decisionTenant: string) => {
      setActingIds((prev) => new Set(prev).add(id));
      setActionError(null);
      try {
        await denyDecision(decisionTenant, id);
        await decisionsPoll.refresh();
      } catch (err) {
        const msg = err instanceof Error ? err.message : "Failed to deny decision";
        setActionError(msg);
      } finally {
        setActingIds((prev) => {
          const next = new Set(prev);
          next.delete(id);
          return next;
        });
      }
    },
    [decisionsPoll],
  );

  const pendingDecisions = useMemo(() => {
    if (!data) return [];
    return data.decisions.filter((d) => d.status === "pending");
  }, [data]);

  const resolvedDecisions = useMemo(() => {
    if (!data) return [];
    return data.decisions.filter((d) => d.status !== "pending");
  }, [data]);

  const pendingGroups = useMemo(
    () => groupDecisions(pendingDecisions, groupingStrategy),
    [pendingDecisions, groupingStrategy],
  );

  const selectedDecisions = useMemo(
    () => pendingDecisions.filter((d) => selectedIds.has(d.id)),
    [pendingDecisions, selectedIds],
  );

  const handleToggleSelect = useCallback((id: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const handleToggleGroup = useCallback((ids: string[]) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      const allSelected = ids.every((id) => next.has(id));
      if (allSelected) {
        for (const id of ids) next.delete(id);
      } else {
        for (const id of ids) next.add(id);
      }
      return next;
    });
  }, []);

  const handleBatchApprove = useCallback(
    async (ids: string[], matrix: PolicyScopeMatrix) => {
      setActionError(null);
      for (const id of ids) {
        setActingIds((prev) => new Set(prev).add(id));
      }
      const allDecisions = data?.decisions || [];
      const results = await Promise.allSettled(
        ids.map((id) => {
          const decision = allDecisions.find((d) => d.id === id);
          return approveDecision(decision?.tenant || "", id, matrix);
        }),
      );
      const succeeded = results.filter((r) => r.status === "fulfilled").length;
      const failed = results.filter((r) => r.status === "rejected").length;
      if (failed > 0) {
        const firstError = results.find((r) => r.status === "rejected") as PromiseRejectedResult;
        setActionError(`${failed} approval(s) failed: ${firstError.reason}`);
      }
      setSelectedIds(new Set());
      for (const id of ids) {
        setActingIds((prev) => {
          const next = new Set(prev);
          next.delete(id);
          return next;
        });
      }
      await decisionsPoll.refresh();
      return { succeeded, failed };
    },
    [decisionsPoll, data],
  );

  const groupedHistory = useMemo(
    () => groupByDate(resolvedDecisions, (d) => d.decided_at),
    [resolvedDecisions],
  );

  const showTenantBadge = tenant === ALL_TENANTS;

  if (initialLoading) {
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

  if (initialError) {
    return (
      <ErrorDisplay
        title="Cannot load decisions"
        message={initialError}
        retry={loadInitial}
      />
    );
  }

  return (
    <div className="animate-fade-in">
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl text-[var(--color-text-primary)] tracking-tight font-serif">
            Decisions
          </h1>
          <p className="text-sm text-[var(--color-text-muted)] mt-0.5">
            Authorization decisions requiring approval or review
          </p>
        </div>
        <div className="flex items-center gap-3">
          {liveDecisions.length > 0 && (
            <div className="flex items-center gap-1.5">
              <div className="w-1.5 h-1.5 bg-[var(--color-accent-pink)] rounded-full animate-pulse" />
              <span className="text-xs text-[var(--color-text-secondary)] font-mono">
                {liveDecisions.length} live
              </span>
            </div>
          )}
          <Select value={tenant} onValueChange={setTenant}>
            <SelectTrigger className="bg-[var(--color-bg-surface)] text-[var(--color-text-secondary)] text-xs rounded-[2px] w-36 h-7">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL_TENANTS}>All tenants</SelectItem>
              {tenants.map((t) => (
                <SelectItem key={t} value={t}>{t}</SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Select value={statusFilter} onValueChange={setStatusFilter}>
            <SelectTrigger className="bg-[var(--color-bg-surface)] text-[var(--color-text-secondary)] text-xs rounded-[2px] w-36 h-7">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All statuses</SelectItem>
              <SelectItem value="pending">Pending</SelectItem>
              <SelectItem value="approved">Approved</SelectItem>
              <SelectItem value="denied">Denied</SelectItem>
              <SelectItem value="expired">Expired</SelectItem>
            </SelectContent>
          </Select>
          {pendingDecisions.length > 1 && (
            <>
              <Button
                variant={batchMode ? "secondary" : "outline"}
                size="sm"
                onClick={() => {
                  setBatchMode(!batchMode);
                  setSelectedIds(new Set());
                }}
                className={`rounded-[2px] text-xs ${
                  batchMode
                    ? "bg-[var(--color-accent-teal-dim)] text-[var(--color-accent-teal)] ring-1 ring-[var(--color-accent-teal)]"
                    : "bg-[var(--color-bg-elevated)] text-[var(--color-text-secondary)]"
                }`}
              >
                Batch
              </Button>
              {batchMode && (
                <Select value={groupingStrategy} onValueChange={(v) => setGroupingStrategy(v as GroupingStrategy)}>
                  <SelectTrigger className="bg-[var(--color-bg-surface)] text-[var(--color-text-secondary)] text-xs rounded-[2px] w-44 h-7">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="action_resource">By action + type</SelectItem>
                    <SelectItem value="agent_action">By agent + action</SelectItem>
                    <SelectItem value="agent_type_action">By agent type + action</SelectItem>
                  </SelectContent>
                </Select>
              )}
            </>
          )}
          {resolvedDecisions.length > 0 && (
            <Button
              variant="outline"
              size="sm"
              onClick={() => exportDecisions(data?.decisions ?? [])}
              className="rounded-[2px] bg-[var(--color-bg-elevated)] text-[var(--color-text-secondary)] text-xs"
            >
              Export
            </Button>
          )}
        </div>
      </div>

      {/* Stats row */}
      <div className="grid grid-cols-4 gap-3 mb-6">
        <StatCard
          label="Pending"
          value={data?.pending_count ?? 0}
          color={
            data && data.pending_count > 0 ? "text-[var(--color-accent-pink)]" : undefined
          }
        />
        <StatCard
          label="Approved"
          value={data?.approved_count ?? 0}
          color="text-[var(--color-accent-teal)]"
        />
        <StatCard
          label="Denied"
          value={data?.denied_count ?? 0}
          color={
            data && data.denied_count > 0 ? "text-[var(--color-accent-pink)]" : undefined
          }
        />
        <StatCard label="Total" value={data?.total ?? 0} />
      </div>

      {/* Polling error banner */}
      {decisionsPoll.error && !data && (
        <Alert variant="destructive" className="mb-4 rounded-[2px] bg-[var(--color-accent-pink-dim)] border-[var(--color-accent-pink)]/20 flex items-center justify-between gap-2 py-2.5">
          <AlertDescription className="text-[var(--color-accent-pink)] col-start-1">Failed to load decisions: {decisionsPoll.error}</AlertDescription>
          <Button variant="ghost" size="sm" onClick={() => decisionsPoll.refresh()} className="rounded-[2px] text-[var(--color-accent-teal)] text-xs flex-shrink-0">Retry</Button>
        </Alert>
      )}

      {/* Action error banner */}
      {actionError && (
        <Alert variant="destructive" className="mb-4 rounded-[2px] bg-[var(--color-accent-pink-dim)] border-[var(--color-accent-pink)]/20 flex items-center justify-between gap-2 py-2.5">
          <AlertDescription className="text-[var(--color-accent-pink)] col-start-1">{actionError}</AlertDescription>
          <Button variant="ghost" size="sm" onClick={() => setActionError(null)} className="rounded-[2px] text-[var(--color-accent-pink)] text-xs flex-shrink-0" aria-label="Dismiss error">Dismiss</Button>
        </Alert>
      )}

      {/* Pending Decisions */}
      {pendingDecisions.length > 0 && (
        <div className={`mb-6 ${batchMode && selectedDecisions.length > 0 ? "pb-24" : ""}`}>
          <div className="flex items-center gap-2 mb-3">
            <div className="w-1.5 h-1.5 bg-[var(--color-accent-pink)] rounded-full animate-pulse" />
            <h2 className="text-base font-semibold text-[var(--color-text-primary)] tracking-tight">
              Pending Decisions
            </h2>
            <span className="text-[10px] font-mono text-[var(--color-text-muted)]">
              {pendingDecisions.length}
            </span>
          </div>

          {batchMode ? (
            <div className="grid gap-3">
              {Array.from(pendingGroups.entries()).map(([key, decisions]) => (
                <DecisionGroup
                  key={key}
                  groupKey={key}
                  strategy={groupingStrategy}
                  decisions={decisions}
                  selectedIds={selectedIds}
                  onToggleSelect={handleToggleSelect}
                  onToggleGroup={handleToggleGroup}
                />
              ))}
            </div>
          ) : (
            <div className="grid gap-3">
              {pendingDecisions.map((d) => (
                <DecisionCard
                  key={d.id}
                  decision={d}
                  onApprove={handleApprove}
                  onDeny={handleDeny}
                  acting={actingIds.has(d.id)}
                  showTenant={showTenantBadge}
                />
              ))}
            </div>
          )}
        </div>
      )}

      {pendingDecisions.length === 0 && statusFilter === "all" && (
        <Card className="glass rounded-[2px] border-0 gap-0 mb-6">
          <CardContent className="p-6 text-center">
            <p className="text-sm text-[var(--color-text-secondary)]">
              No pending decisions. All clear.
            </p>
          </CardContent>
        </Card>
      )}

      {/* Batch approve bar */}
      {batchMode && (
        <BatchApproveBar
          selectedDecisions={selectedDecisions}
          onApprove={handleBatchApprove}
          onClear={() => setSelectedIds(new Set())}
        />
      )}

      {/* History Table — grouped by date */}
      {resolvedDecisions.length > 0 && (
        <div>
          <h2 className="text-base font-semibold text-[var(--color-text-primary)] mb-3 tracking-tight">
            Decision History
          </h2>
          {Array.from(groupedHistory.entries()).map(
            ([bucket, decisions]) => (
              <div key={bucket} className="mb-4">
                <div className="flex items-center gap-2 mb-2">
                  <span className="text-[11px] font-medium text-[var(--color-text-secondary)] uppercase tracking-wider">
                    {bucket}
                  </span>
                  <div className="flex-1 h-px bg-[var(--color-bg-elevated)]" />
                  <span className="text-[10px] font-mono text-[var(--color-text-muted)]">
                    {decisions.length}
                  </span>
                </div>
                <Card className="glass rounded-[2px] border-0 gap-0 overflow-hidden">
                  <ScrollArea className="max-h-96">
                  <Table className="text-[13px]">
                    <TableHeader className="sticky top-0 bg-[color-mix(in_srgb,var(--color-bg-surface)_90%,transparent)] backdrop-blur-sm z-10">
                      <TableRow className="border-b border-[var(--color-border)] hover:bg-transparent">
                        {showTenantBadge && (
                          <TableHead className="px-3 py-2 text-[var(--color-text-muted)] text-xs uppercase tracking-wider whitespace-nowrap h-auto">
                            Tenant
                          </TableHead>
                        )}
                        <TableHead className="px-3 py-2 text-[var(--color-text-muted)] text-xs uppercase tracking-wider whitespace-nowrap h-auto">
                          Agent
                        </TableHead>
                        <TableHead className="px-3 py-2 text-[var(--color-text-muted)] text-xs uppercase tracking-wider whitespace-nowrap h-auto">
                          Action
                        </TableHead>
                        <TableHead className="px-3 py-2 text-[var(--color-text-muted)] text-xs uppercase tracking-wider whitespace-nowrap h-auto">
                          Resource
                        </TableHead>
                        <TableHead className="px-3 py-2 text-[var(--color-text-muted)] text-xs uppercase tracking-wider whitespace-nowrap h-auto">
                          Status
                        </TableHead>
                        <TableHead className="px-3 py-2 text-[var(--color-text-muted)] text-xs uppercase tracking-wider whitespace-nowrap h-auto">
                          Scope
                        </TableHead>
                        <TableHead className="px-3 py-2 text-right text-[var(--color-text-muted)] text-xs uppercase tracking-wider whitespace-nowrap h-auto">
                          Decided
                        </TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {decisions.map((d, i) => (
                        <HistoryRow
                          key={d.id}
                          decision={d}
                          showTenant={showTenantBadge}
                          even={i % 2 === 1}
                        />
                      ))}
                    </TableBody>
                  </Table>
                  </ScrollArea>
                </Card>
              </div>
            ),
          )}
        </div>
      )}
    </div>
  );
}
