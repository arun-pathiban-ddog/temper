"use client";

import { useState, useMemo, useCallback, useEffect } from "react";
import { fetchWasmModules, fetchWasmInvocations } from "@/lib/api";
import { useSSERefresh } from "@/lib/hooks";
import type { WasmModulesResponse, WasmInvocationsResponse } from "@/lib/types";
import ErrorDisplay from "@/components/ErrorDisplay";
import StatCard from "@/components/StatCard";
import { rateColor, rateBgColor } from "@/lib/utils";
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Card, CardContent } from "@/components/ui/card";
import { ScrollArea } from "@/components/ui/scroll-area";

export default function IntegrationsPage() {
  const [initialLoading, setInitialLoading] = useState(true);
  const [initialError, setInitialError] = useState<string | null>(null);
  const [moduleFilter, setModuleFilter] = useState<string>("all");
  const [expandedInvocation, setExpandedInvocation] = useState<number | null>(null);

  const loadInitial = useCallback(async () => {
    setInitialLoading(true);
    setInitialError(null);
    try {
      await Promise.all([fetchWasmModules(), fetchWasmInvocations()]);
    } catch (err) {
      setInitialError(err instanceof Error ? err.message : "Failed to load integrations data");
    } finally {
      setInitialLoading(false);
    }
  }, []);

  useEffect(() => {
    loadInitial();
  }, [loadInitial]);

  const modulesPoll = useSSERefresh<WasmModulesResponse>({
    fetcher: fetchWasmModules,
    sseKinds: ["Entities"],
    enabled: !initialLoading && !initialError,
  });

  const invocationsPoll = useSSERefresh<WasmInvocationsResponse>({
    fetcher: () =>
      fetchWasmInvocations(
        moduleFilter !== "all" ? { module_name: moduleFilter, limit: 100 } : { limit: 100 },
      ),
    sseKinds: ["Entities"],
    enabled: !initialLoading && !initialError,
  });

  const modules = modulesPoll.data;
  const invocations = invocationsPoll.data;
  // Derive stats
  const totalModules = modules?.total ?? 0;
  const totalInvocations = useMemo(() => {
    if (!modules?.modules) return 0;
    return modules.modules.reduce((sum, m) => sum + m.total_invocations, 0);
  }, [modules]);

  const overallSuccessRate = useMemo(() => {
    if (!modules?.modules || totalInvocations === 0) return 0;
    const totalSuccess = modules.modules.reduce((sum, m) => sum + m.success_count, 0);
    return Math.round((totalSuccess / totalInvocations) * 100);
  }, [modules, totalInvocations]);

  // Derive module names for filter
  const moduleNames = useMemo(() => {
    if (!modules?.modules) return [];
    return modules.modules.map((m) => m.module_name).sort();
  }, [modules]);

  if (initialLoading) {
    return (
      <div>
        <Skeleton className="h-6 w-40 rounded-[2px] mb-1.5" />
        <Skeleton className="h-3.5 w-72 rounded-[2px] mb-6" />
        <div className="grid grid-cols-3 gap-3 mb-6">
          {[0, 1, 2].map((i) => (
            <Card key={i} className="glass rounded-[2px] border-0 gap-0">
              <CardContent className="p-4">
                <Skeleton className="h-3 w-20 rounded-[2px] mb-2" />
                <Skeleton className="h-8 w-10 rounded-[2px]" />
              </CardContent>
            </Card>
          ))}
        </div>
      </div>
    );
  }

  if (initialError) {
    return <ErrorDisplay title="Cannot load integrations" message={initialError} retry={loadInitial} />;
  }

  return (
    <div className="animate-fade-in">
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl text-[var(--color-text-primary)] tracking-tight font-serif">Integrations</h1>
          <p className="text-sm text-[var(--color-text-muted)] mt-0.5">
            WASM modules, invocation history, and success rates
          </p>
        </div>
        <div className="flex items-center gap-3">
          {moduleNames.length > 0 && (
            <Select value={moduleFilter} onValueChange={setModuleFilter}>
              <SelectTrigger className="rounded-[2px] bg-[var(--color-bg-surface)] text-[var(--color-text-secondary)] text-xs h-7 w-40">
                <SelectValue />
              </SelectTrigger>
              <SelectContent className="rounded-[2px]">
                <SelectItem value="all">All modules</SelectItem>
                {moduleNames.map((m) => (
                  <SelectItem key={m} value={m}>{m}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}
        </div>
      </div>

      {/* Stats row */}
      <div className="grid grid-cols-3 gap-3 mb-6">
        <StatCard label="Total Modules" value={totalModules} />
        <StatCard label="Total Invocations" value={totalInvocations} />
        <StatCard
          label="Success Rate"
          value={totalInvocations > 0 ? `${overallSuccessRate}%` : "\u2013"}
          color={totalInvocations > 0 ? rateColor(overallSuccessRate) : undefined}
        />
      </div>

      {/* Modules Table */}
      {modules && modules.modules.length > 0 && (
        <div className="mb-6">
          <h2 className="text-base font-semibold text-[var(--color-text-primary)] mb-3 tracking-tight">Modules</h2>
          <Card className="glass rounded-[2px] border-0 gap-0 overflow-hidden">
            <Table>
              <TableHeader className="sticky top-0 bg-[color-mix(in_srgb,var(--color-bg-surface)_90%,transparent)] backdrop-blur-sm z-10">
                <TableRow className="border-b border-[var(--color-border)] hover:bg-transparent">
                  <TableHead className="h-auto px-3.5 py-2.5 text-[11px] text-[var(--color-text-muted)] font-medium uppercase tracking-wider">Tenant</TableHead>
                  <TableHead className="h-auto px-3.5 py-2.5 text-[11px] text-[var(--color-text-muted)] font-medium uppercase tracking-wider">Name</TableHead>
                  <TableHead className="h-auto px-3.5 py-2.5 text-[11px] text-[var(--color-text-muted)] font-medium uppercase tracking-wider">Hash</TableHead>
                  <TableHead className="h-auto px-3.5 py-2.5 text-[11px] text-[var(--color-text-muted)] font-medium uppercase tracking-wider text-center">Cached</TableHead>
                  <TableHead className="h-auto px-3.5 py-2.5 text-[11px] text-[var(--color-text-muted)] font-medium uppercase tracking-wider text-right">Invocations</TableHead>
                  <TableHead className="h-auto px-3.5 py-2.5 text-[11px] text-[var(--color-text-muted)] font-medium uppercase tracking-wider w-32">Success Rate</TableHead>
                  <TableHead className="h-auto px-3.5 py-2.5 text-[11px] text-[var(--color-text-muted)] font-medium uppercase tracking-wider">Last Used</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {modules.modules.map((mod_, i) => {
                  const rate = mod_.total_invocations > 0
                    ? Math.round(mod_.success_rate * 100)
                    : 0;
                  return (
                    <TableRow
                      key={`${mod_.tenant}-${mod_.module_name}`}
                      className={`border-b border-[var(--color-border)] ${i % 2 === 1 ? "bg-[var(--color-bg-elevated)]" : ""}`}
                    >
                      <TableCell className="px-3.5 py-2.5 text-[12px] text-[var(--color-text-secondary)]">{mod_.tenant}</TableCell>
                      <TableCell className="px-3.5 py-2.5 text-[12px] font-mono text-[var(--color-text-secondary)]">{mod_.module_name}</TableCell>
                      <TableCell className="px-3.5 py-2.5 text-[12px] font-mono text-[var(--color-text-secondary)]">
                        {mod_.sha256_hash.substring(0, 12)}...
                      </TableCell>
                      <TableCell className="px-3.5 py-2.5 text-[12px] text-center">
                        {mod_.cached ? (
                          <span className="text-[10px] font-medium bg-[var(--color-accent-teal-dim)] text-[var(--color-accent-teal)] px-1.5 py-0.5 rounded">
                            cached
                          </span>
                        ) : (
                          <span className="text-[10px] font-medium bg-[var(--color-accent-lime-dim)] text-[var(--color-text-secondary)] px-1.5 py-0.5 rounded">
                            cold
                          </span>
                        )}
                      </TableCell>
                      <TableCell className="px-3.5 py-2.5 text-[12px] text-right font-mono text-[var(--color-text-secondary)]">
                        {mod_.total_invocations}
                      </TableCell>
                      <TableCell className="px-3.5 py-2.5 text-[12px]">
                        {mod_.total_invocations > 0 ? (
                          <div className="flex items-center gap-2">
                            <div className="flex-1 h-1.5 bg-[var(--color-bg-elevated)] rounded-full overflow-hidden">
                              <div
                                className={`h-full rounded-full ${rateBgColor(rate)}`}
                                style={{ width: `${rate}%` }}
                              />
                            </div>
                            <span className={`text-[11px] font-mono ${rateColor(rate)}`}>
                              {rate}%
                            </span>
                          </div>
                        ) : (
                          <span className="text-[11px] text-[var(--color-text-muted)]">{"\u2013"}</span>
                        )}
                      </TableCell>
                      <TableCell className="px-3.5 py-2.5 text-[12px] text-[var(--color-text-secondary)] font-mono">
                        {mod_.last_invoked_at
                          ? new Date(mod_.last_invoked_at).toLocaleTimeString()
                          : "\u2013"}
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </Card>
        </div>
      )}

      {/* Empty state for modules */}
      {modules && modules.modules.length === 0 && (
        <Card className="glass rounded-[2px] border-0 gap-0 mb-6">
          <CardContent className="p-6 text-center">
            <div className="text-[var(--color-text-secondary)] text-sm">No WASM modules uploaded yet.</div>
            <p className="text-[var(--color-text-muted)] text-xs mt-1">
              Upload modules via POST /api/wasm/modules/:name
            </p>
          </CardContent>
        </Card>
      )}

      {/* Recent Invocations */}
      <div>
        <h2 className="text-base font-semibold text-[var(--color-text-primary)] mb-3 tracking-tight">
          Recent Invocations
          {invocations && invocations.total > 0 && (
            <span className="text-[var(--color-text-muted)] font-normal text-[13px] ml-2">{invocations.total}</span>
          )}
        </h2>
        {!invocations || invocations.invocations.length === 0 ? (
          <Card className="glass rounded-[2px] border-0 gap-0">
            <CardContent className="p-6 text-center">
              <p className="text-sm text-[var(--color-text-secondary)]">No invocations recorded yet.</p>
            </CardContent>
          </Card>
        ) : (
          <Card className="glass rounded-[2px] border-0 gap-0 overflow-hidden">
            <ScrollArea className="max-h-96">
            {invocations.invocations.map((inv, i) => {
              const ts = new Date(inv.timestamp);
              const timeStr = ts.toLocaleTimeString();
              const isExpanded = expandedInvocation === i;
              const hasError = !inv.success && inv.error;
              return (
                <div key={`${inv.timestamp}-${i}`}>
                  <div
                    className={`flex items-center gap-3 px-3.5 py-2.5 border-b border-[var(--color-border)] last:border-b-0 ${hasError ? "cursor-pointer hover:bg-[var(--color-bg-elevated)]" : ""}`}
                    onClick={() => hasError && setExpandedInvocation(isExpanded ? null : i)}
                  >
                    <span className="text-[11px] text-[var(--color-text-muted)] font-mono flex-shrink-0 w-20">
                      {timeStr}
                    </span>
                    <span className="font-mono text-[11px] text-[var(--color-text-secondary)] flex-shrink-0">
                      {inv.module_name}
                    </span>
                    <span className="text-[10px] font-mono text-[var(--color-text-muted)] flex-shrink-0">
                      {inv.entity_type}/{inv.entity_id.substring(0, 8)}
                    </span>
                    <span className="text-[11px] text-[var(--color-text-secondary)] font-mono flex-shrink-0">
                      {inv.trigger_action}
                    </span>
                    {inv.callback_action && (
                      <>
                        <span className="text-[var(--color-text-muted)] text-[11px]">&rarr;</span>
                        <span className={`text-[11px] font-mono flex-shrink-0 ${inv.success ? "text-[var(--color-accent-teal)]" : "text-[var(--color-accent-pink)]"}`}>
                          {inv.callback_action}
                        </span>
                      </>
                    )}
                    <span className="ml-auto flex-shrink-0">
                      {inv.success ? (
                        <span className="text-[10px] font-medium bg-[var(--color-accent-teal-dim)] text-[var(--color-accent-teal)] px-1.5 py-0.5 rounded">
                          ok
                        </span>
                      ) : (
                        <span className="text-[10px] font-medium bg-[var(--color-accent-pink-dim)] text-[var(--color-accent-pink)] px-1.5 py-0.5 rounded">
                          fail
                        </span>
                      )}
                    </span>
                    <span className="text-[10px] font-mono text-[var(--color-text-muted)] flex-shrink-0 w-12 text-right">
                      {inv.duration_ms}ms
                    </span>
                    {hasError && (
                      <span className="text-[11px] text-[var(--color-text-muted)] flex-shrink-0">
                        {isExpanded ? "\u25B4" : "\u25BE"}
                      </span>
                    )}
                  </div>
                  {isExpanded && hasError && (
                    <div className="px-3.5 py-3 bg-[var(--color-accent-pink-dim)] border-b border-[var(--color-border)]">
                      <div className="text-[10px] text-[var(--color-text-muted)] uppercase tracking-wider mb-1.5">Error Details</div>
                      <pre className="text-[12px] text-[var(--color-accent-pink)] font-mono whitespace-pre-wrap break-all leading-relaxed">
                        {inv.error}
                      </pre>
                      <div className="mt-2 flex gap-4 text-[10px] text-[var(--color-text-muted)]">
                        <span>Tenant: {inv.tenant}</span>
                        <span>Entity: {inv.entity_type}/{inv.entity_id}</span>
                        <span>Trigger: {inv.trigger_action}</span>
                        <span>{ts.toLocaleString()}</span>
                      </div>
                    </div>
                  )}
                </div>
              );
            })}
            </ScrollArea>
          </Card>
        )}
      </div>
    </div>
  );
}
