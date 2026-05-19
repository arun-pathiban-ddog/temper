"use client";

import { useEffect, useState, useCallback } from "react";
import Link from "next/link";
import { ExternalLink } from "lucide-react";
import { fetchEntityHistory } from "@/lib/api";
import type { EntityHistory } from "@/lib/types";
import StatusBadge from "@/components/StatusBadge";
import { Sheet, SheetContent, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Skeleton } from "@/components/ui/skeleton";
import { Card } from "@/components/ui/card";

interface EntityDetailPanelProps {
  entityType: string;
  entityId: string;
  tenant?: string;
  onClose: () => void;
}

function FieldTable({ entries }: { entries: [string, unknown][] }) {
  return (
    <Card className="glass rounded-[2px] border-0 gap-0 overflow-hidden">
      {entries.map(([key, value]) => (
        <div key={key} className="flex items-center justify-between px-3 py-2 border-b border-[var(--color-border)] last:border-b-0">
          <span className="text-[11px] text-[var(--color-text-secondary)] font-mono">{key}</span>
          <span className="text-[11px] text-[var(--color-text-secondary)] font-mono truncate ml-3 max-w-[200px]">{String(value)}</span>
        </div>
      ))}
    </Card>
  );
}

export default function EntityDetailPanel({ entityType, entityId, tenant, onClose }: EntityDetailPanelProps) {
  const [history, setHistory] = useState<EntityHistory | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await fetchEntityHistory(entityType, entityId, tenant);
      setHistory(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load entity");
    } finally {
      setLoading(false);
    }
  }, [entityType, entityId, tenant]);

  useEffect(() => { load(); }, [load]);

  return (
    <Sheet open onOpenChange={(open) => { if (!open) onClose(); }}>
      <SheetContent side="right" className="w-96 p-0 rounded-none border-[var(--color-border)] bg-[var(--color-bg-primary)]/95 backdrop-blur-sm">
        <SheetHeader className="sticky top-0 bg-[var(--color-bg-primary)]/95 backdrop-blur-sm z-10 px-5 py-4 border-b border-[var(--color-border)]">
          <div className="min-w-0">
            <div className="text-[10px] text-[var(--color-text-muted)] uppercase tracking-wider mb-0.5">{entityType}</div>
            <SheetTitle className="text-sm font-mono text-[var(--color-text-primary)] truncate font-normal">
              {entityId}
            </SheetTitle>
          </div>
        </SheetHeader>

        <ScrollArea className="h-[calc(100vh-80px)]">
          <div className="px-5 py-4">
            {loading && (
              <div className="space-y-3">
                <Skeleton className="h-4 w-24 rounded-[2px]" />
                <Skeleton className="h-20 w-full rounded-[2px]" />
                <Skeleton className="h-4 w-32 rounded-[2px]" />
                <Skeleton className="h-32 w-full rounded-[2px]" />
              </div>
            )}

            {!loading && error && (
              <div className="text-center py-8">
                <p className="text-[var(--color-accent-pink)] text-[13px] mb-2">{error}</p>
                <Button variant="link" size="sm" onClick={load} className="text-[var(--color-accent-teal)] p-0 h-auto">
                  Retry
                </Button>
              </div>
            )}

            {!loading && history && (
              <div className="space-y-5">
                <div>
                  <div className="text-[10px] text-[var(--color-text-muted)] uppercase tracking-wider mb-1.5">Current State</div>
                  <StatusBadge status={history.current_state} />
                </div>

                {history.fields && Object.keys(history.fields).length > 0 && (
                  <div>
                    <div className="text-[10px] text-[var(--color-text-muted)] uppercase tracking-wider mb-1.5">Fields</div>
                    <FieldTable entries={Object.entries(history.fields)} />
                  </div>
                )}

                {history.counters && Object.keys(history.counters).length > 0 && (
                  <div>
                    <div className="text-[10px] text-[var(--color-text-muted)] uppercase tracking-wider mb-1.5">Counters</div>
                    <Card className="glass rounded-[2px] border-0 gap-0 overflow-hidden">
                      {Object.entries(history.counters).map(([key, value]) => (
                        <div key={key} className="flex items-center justify-between px-3 py-2 border-b border-[var(--color-border)] last:border-b-0">
                          <span className="text-[11px] text-[var(--color-text-secondary)] font-mono">{key}</span>
                          <span className="text-[11px] text-[var(--color-accent-teal)] font-mono">{value}</span>
                        </div>
                      ))}
                    </Card>
                  </div>
                )}

                {history.booleans && Object.keys(history.booleans).length > 0 && (
                  <div>
                    <div className="text-[10px] text-[var(--color-text-muted)] uppercase tracking-wider mb-1.5">Booleans</div>
                    <Card className="glass rounded-[2px] border-0 gap-0 overflow-hidden">
                      {Object.entries(history.booleans).map(([key, value]) => (
                        <div key={key} className="flex items-center justify-between px-3 py-2 border-b border-[var(--color-border)] last:border-b-0">
                          <span className="text-[11px] text-[var(--color-text-secondary)] font-mono">{key}</span>
                          <span className={`text-[11px] font-mono ${value ? "text-[var(--color-accent-teal)]" : "text-[var(--color-text-muted)]"}`}>
                            {value ? "true" : "false"}
                          </span>
                        </div>
                      ))}
                    </Card>
                  </div>
                )}

                {history.lists && Object.keys(history.lists).length > 0 && (
                  <div>
                    <div className="text-[10px] text-[var(--color-text-muted)] uppercase tracking-wider mb-1.5">Lists</div>
                    <Card className="glass rounded-[2px] border-0 gap-0 overflow-hidden">
                      {Object.entries(history.lists).map(([key, value]) => (
                        <div key={key} className="px-3 py-2 border-b border-[var(--color-border)] last:border-b-0">
                          <div className="text-[11px] text-[var(--color-text-secondary)] font-mono mb-1">{key}</div>
                          {value.length === 0 ? (
                            <span className="text-[10px] text-[var(--color-text-muted)]">(empty)</span>
                          ) : (
                            <div className="flex flex-wrap gap-1">
                              {value.map((item, j) => (
                                <Badge key={j} variant="outline" className="text-[10px] font-mono bg-[var(--color-bg-elevated)] text-[var(--color-text-secondary)] border-transparent">
                                  {item}
                                </Badge>
                              ))}
                            </div>
                          )}
                        </div>
                      ))}
                    </Card>
                  </div>
                )}

                {history.events.length > 0 && (
                  <div>
                    <div className="text-[10px] text-[var(--color-text-muted)] uppercase tracking-wider mb-1.5">
                      Recent Events ({history.events.length})
                    </div>
                    <Card className="glass rounded-[2px] border-0 gap-0 overflow-hidden"><ScrollArea className="max-h-48">
                      {[...history.events].reverse().slice(0, 20).map((event, i) => (
                        <div key={i} className="flex items-center gap-2 px-3 py-2 border-b border-[var(--color-border)] last:border-b-0">
                          <span className="text-[10px] text-[var(--color-accent-teal)] font-mono flex-shrink-0">{event.action}</span>
                          <span className="text-[var(--color-text-muted)] text-[10px]">&rarr;</span>
                          <span className="text-[10px] text-[var(--color-text-secondary)] font-mono">{event.to_state}</span>
                        </div>
                      ))}
                    </ScrollArea></Card>
                  </div>
                )}

                <div className="pt-2">
                  <Button variant="secondary" size="sm" asChild className="w-full rounded-[2px] text-[12px]">
                    <Link href={`/entities/${entityType}/${entityId}${tenant ? `?tenant=${encodeURIComponent(tenant)}` : ""}`}>
                      Open full page
                      <ExternalLink className="ml-1.5 h-3.5 w-3.5" />
                    </Link>
                  </Button>
                </div>
              </div>
            )}
          </div>
        </ScrollArea>
      </SheetContent>
    </Sheet>
  );
}
