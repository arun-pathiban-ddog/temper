"use client";

import { useState, useCallback, useMemo } from "react";
import {
  fetchFeatureRequests,
  updateFeatureRequest,
} from "@/lib/api";
import { useSSERefresh } from "@/lib/hooks";
import type {
  FeatureRequest,
  FeatureRequestDisposition,
  PlatformGapCategory,
} from "@/lib/types";
import ErrorDisplay from "@/components/ErrorDisplay";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Card, CardContent } from "@/components/ui/card";

const categoryColors: Record<PlatformGapCategory, string> = {
  MissingMethod: "bg-[var(--color-accent-lime-dim)] text-[var(--color-accent-lime)]",
  GovernanceBlocked: "bg-[var(--color-accent-pink-dim)] text-[var(--color-accent-pink)]",
  UnsupportedIntegration: "bg-[var(--color-accent-pink-dim)] text-[var(--color-accent-pink)]",
  MissingCapability: "bg-[var(--color-accent-teal-dim)] text-[var(--color-accent-teal)]",
};

const dispositionColors: Record<FeatureRequestDisposition, string> = {
  Open: "bg-[var(--color-accent-pink-dim)] text-[var(--color-accent-pink)]",
  Acknowledged: "bg-blue-500/15 text-blue-400",
  Planned: "bg-[var(--color-accent-lime-dim)] text-[var(--color-accent-lime)]",
  WontFix: "bg-[var(--color-accent-lime-dim)] text-[var(--color-text-secondary)]",
  Resolved: "bg-[var(--color-accent-teal-dim)] text-[var(--color-accent-teal)]",
};

const DISPOSITIONS: FeatureRequestDisposition[] = [
  "Open",
  "Acknowledged",
  "Planned",
  "WontFix",
  "Resolved",
];

type FilterTab = "all" | FeatureRequestDisposition;

function FeatureRequestCard({
  request,
  onUpdate,
  acting,
}: {
  request: FeatureRequest;
  onUpdate: (id: string, disposition: FeatureRequestDisposition, notes?: string) => void;
  acting: boolean;
}) {
  const [showNotes, setShowNotes] = useState(false);
  const [notes, setNotes] = useState(request.developer_notes ?? "");
  const createdAt = new Date(request.created_at).toLocaleString();

  return (
    <Card className="rounded-[2px] border-[var(--color-border)] gap-0 animate-fade-in">
      <CardContent className="p-4">
      {/* Header row */}
      <div className="flex items-start justify-between mb-3">
        <div className="flex items-center gap-2 flex-wrap">
          <span
            className={`text-xs font-medium px-1.5 py-0.5 rounded ${
              categoryColors[request.category] ?? "bg-[var(--color-accent-lime-dim)] text-[var(--color-text-secondary)]"
            }`}
          >
            {request.category}
          </span>
          <span
            className={`text-xs font-medium px-1.5 py-0.5 rounded ${
              dispositionColors[request.disposition] ?? "bg-[var(--color-accent-lime-dim)] text-[var(--color-text-secondary)]"
            }`}
          >
            {request.disposition}
          </span>
          <span className="text-xs font-mono text-[var(--color-text-muted)]">
            {request.id.slice(0, 12)}
          </span>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-xs font-mono text-[var(--color-text-secondary)]">
            {request.frequency}x
          </span>
        </div>
      </div>

      {/* Description */}
      <p className="text-sm text-[var(--color-text-secondary)] mb-3">{request.description}</p>

      {/* Trajectory refs */}
      {request.trajectory_refs.length > 0 && (
        <div className="flex items-center gap-2 mb-3">
          <span className="text-[10px] text-[var(--color-text-muted)] uppercase tracking-wider">
            Trajectories
          </span>
          <div className="flex flex-wrap gap-1">
            {request.trajectory_refs.slice(0, 5).map((ref) => (
              <span
                key={ref}
                className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-[var(--color-bg-elevated)] text-[var(--color-text-secondary)]"
              >
                {ref.slice(0, 12)}
              </span>
            ))}
            {request.trajectory_refs.length > 5 && (
              <span className="text-[10px] font-mono text-[var(--color-text-muted)]">
                +{request.trajectory_refs.length - 5} more
              </span>
            )}
          </div>
        </div>
      )}

      {/* Developer notes */}
      {request.developer_notes && !showNotes && (
        <div className="mb-3 px-3 py-2 bg-black/20 rounded text-xs text-[var(--color-text-secondary)]">
          {request.developer_notes}
        </div>
      )}

      {/* Timestamp */}
      <div className="text-[10px] text-[var(--color-text-muted)] mb-3 font-mono">{createdAt}</div>

      {/* Actions */}
      <div className="flex items-center gap-2 pt-2 border-t border-[var(--color-border)]">
        {DISPOSITIONS.filter((d) => d !== request.disposition).map((d) => (
          <Button
            key={d}
            variant="ghost"
            size="sm"
            onClick={() => onUpdate(request.id, d, showNotes ? notes : undefined)}
            disabled={acting}
            className={`rounded-[2px] px-2.5 py-1 text-[11px] h-auto transition-colors disabled:opacity-40 disabled:cursor-not-allowed ${
              d === "Resolved"
                ? "bg-[var(--color-accent-teal-dim)] hover:bg-[var(--color-accent-teal-dim)] text-[var(--color-accent-teal)]"
                : d === "WontFix"
                  ? "bg-[var(--color-accent-lime-dim)] hover:bg-[var(--color-accent-lime-dim)] text-[var(--color-text-secondary)]"
                  : d === "Planned"
                    ? "bg-[var(--color-accent-lime-dim)] hover:bg-[var(--color-accent-lime-dim)] text-[var(--color-accent-lime)]"
                    : d === "Acknowledged"
                      ? "bg-blue-500/20 hover:bg-blue-500/30 text-blue-400"
                      : "bg-[var(--color-accent-pink-dim)] hover:bg-[var(--color-accent-pink-dim)] text-[var(--color-accent-pink)]"
            }`}
          >
            {d === "WontFix" ? "Won't Fix" : d}
          </Button>
        ))}
        <Button
          variant="ghost"
          size="sm"
          onClick={() => setShowNotes(!showNotes)}
          className="rounded-[2px] ml-auto text-[11px] h-auto text-[var(--color-text-muted)] hover:text-[var(--color-text-secondary)] transition-colors"
        >
          {showNotes ? "Hide Notes" : "Add Notes"}
        </Button>
      </div>

      {/* Notes input */}
      {showNotes && (
        <div className="mt-3 flex gap-2">
          <Input
            type="text"
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
            placeholder="Developer notes..."
            className="rounded-[2px] flex-1 bg-black/30 border border-[var(--color-border)] px-2.5 py-1.5 text-xs text-[var(--color-text-secondary)] placeholder-[var(--color-text-muted)] focus:border-[var(--color-accent-teal)]/30 h-auto"
          />
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              onUpdate(request.id, request.disposition, notes);
              setShowNotes(false);
            }}
            disabled={acting}
            className="rounded-[2px] px-2.5 py-1.5 bg-[var(--color-accent-teal-dim)] hover:bg-[var(--color-accent-teal-dim)] text-[var(--color-accent-teal)] text-xs h-auto transition-colors disabled:opacity-40"
          >
            Save
          </Button>
        </div>
      )}
      </CardContent>
    </Card>
  );
}

export default function FeatureRequestsPage() {
  const [activeTab, setActiveTab] = useState<FilterTab>("all");
  const [acting, setActing] = useState(false);

  const featuresPoll = useSSERefresh<FeatureRequest[]>({
    fetcher: fetchFeatureRequests,
    sseKinds: ["FeatureRequests"],
  });
  const handleUpdate = useCallback(
    async (id: string, disposition: FeatureRequestDisposition, notes?: string) => {
      setActing(true);
      try {
        const update: { disposition?: FeatureRequestDisposition; developer_notes?: string } = {
          disposition,
        };
        if (notes !== undefined) {
          update.developer_notes = notes;
        }
        const ok = await updateFeatureRequest(id, update);
        if (ok) {
          await featuresPoll.refresh();
        }
      } catch {
        // Silently handled by polling
      } finally {
        setActing(false);
      }
    },
    [featuresPoll],
  );

  const requests = featuresPoll.data;

  const filteredRequests = useMemo(() => {
    if (!requests) return [];
    if (activeTab === "all") return requests;
    return requests.filter((r) => r.disposition === activeTab);
  }, [requests, activeTab]);

  const counts = useMemo(() => {
    if (!requests) return { total: 0, open: 0, acknowledged: 0, planned: 0, wontfix: 0, resolved: 0 };
    return {
      total: requests.length,
      open: requests.filter((r) => r.disposition === "Open").length,
      acknowledged: requests.filter((r) => r.disposition === "Acknowledged").length,
      planned: requests.filter((r) => r.disposition === "Planned").length,
      wontfix: requests.filter((r) => r.disposition === "WontFix").length,
      resolved: requests.filter((r) => r.disposition === "Resolved").length,
    };
  }, [requests]);

  if (featuresPoll.loading && !requests) {
    return (
      <div>
        <Skeleton className="h-6 w-48 mb-1.5" />
        <Skeleton className="h-3.5 w-72 mb-6" />
        <div className="grid grid-cols-4 gap-3 mb-6">
          {[0, 1, 2, 3].map((i) => (
            <Card key={i} className="rounded-[2px] border-[var(--color-border)] gap-0">
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

  if (featuresPoll.error && !requests) {
    return (
      <ErrorDisplay
        title="Cannot load feature requests"
        message={featuresPoll.error}
        retry={() => featuresPoll.refresh()}
      />
    );
  }

  return (
    <div className="animate-fade-in">
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl text-[var(--color-text-primary)] tracking-tight font-serif">
            Feature Requests
          </h1>
          <p className="text-sm text-[var(--color-text-muted)] mt-0.5">
            Platform gaps detected from trajectory analysis and agent feedback
          </p>
        </div>
        <div className="flex items-center gap-3">
          {counts.open > 0 && (
            <div className="flex items-center gap-1.5">
              <div className="w-2 h-2 bg-[var(--color-accent-pink)] rounded-full" />
              <span className="text-xs text-[var(--color-accent-pink)]">
                {counts.open} open
              </span>
            </div>
          )}
        </div>
      </div>

      {/* Summary Cards */}
      <div className="grid grid-cols-4 gap-3 mb-6">
        <Card className="rounded-[2px] border-[var(--color-border)] gap-0">
          <CardContent className="p-4">
            <div className="text-xs text-[var(--color-text-muted)]">Open</div>
            <div className={`text-4xl font-bold font-mono mt-0.5 ${counts.open > 0 ? "text-[var(--color-accent-pink)]" : "text-[var(--color-text-primary)]"}`}>
              {counts.open}
            </div>
          </CardContent>
        </Card>
        <Card className="rounded-[2px] border-[var(--color-border)] gap-0">
          <CardContent className="p-4">
            <div className="text-xs text-[var(--color-text-muted)]">Planned</div>
            <div className="text-4xl font-bold font-mono mt-0.5 text-[var(--color-accent-lime)]">
              {counts.planned}
            </div>
          </CardContent>
        </Card>
        <Card className="rounded-[2px] border-[var(--color-border)] gap-0">
          <CardContent className="p-4">
            <div className="text-xs text-[var(--color-text-muted)]">Resolved</div>
            <div className="text-4xl font-bold font-mono mt-0.5 text-[var(--color-accent-teal)]">
              {counts.resolved}
            </div>
          </CardContent>
        </Card>
        <Card className="rounded-[2px] border-[var(--color-border)] gap-0">
          <CardContent className="p-4">
            <div className="text-xs text-[var(--color-text-muted)]">Total</div>
            <div className="text-4xl font-bold font-mono mt-0.5 text-[var(--color-text-primary)]">
              {counts.total}
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Filter Tabs */}
      <Tabs value={activeTab} onValueChange={(v) => setActiveTab(v as FilterTab)} className="mb-4">
        <TabsList variant="line" className="w-full justify-start rounded-none border-b border-[var(--color-border)] bg-transparent h-auto p-0">
          {(["all", ...DISPOSITIONS] as FilterTab[]).map((tab) => (
            <TabsTrigger
              key={tab}
              value={tab}
              className="rounded-none border-0 border-b-2 -mb-px px-3 py-2 text-xs data-[state=active]:border-[var(--color-accent-teal)] data-[state=active]:text-[var(--color-accent-teal)] data-[state=inactive]:border-transparent data-[state=inactive]:text-[var(--color-text-muted)] hover:text-[var(--color-text-secondary)] bg-transparent shadow-none"
            >
              {tab === "all" ? "All" : tab === "WontFix" ? "Won't Fix" : tab}
            </TabsTrigger>
          ))}
        </TabsList>
      </Tabs>

      {/* Request Cards */}
      {filteredRequests.length === 0 ? (
        <Card className="glass rounded-[2px] border-0 gap-0">
          <CardContent className="p-6 text-center">
            <p className="text-sm text-[var(--color-text-secondary)]">
              {activeTab === "all"
                ? "No feature requests yet. They will appear as platform gaps are detected."
                : `No feature requests with disposition "${activeTab === "WontFix" ? "Won't Fix" : activeTab}".`}
            </p>
          </CardContent>
        </Card>
      ) : (
        <div className="grid gap-3">
          {filteredRequests
            .sort((a, b) => b.frequency - a.frequency)
            .map((request) => (
              <FeatureRequestCard
                key={request.id}
                request={request}
                onUpdate={handleUpdate}
                acting={acting}
              />
            ))}
        </div>
      )}
    </div>
  );
}
