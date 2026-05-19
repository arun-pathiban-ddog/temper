"use client";

import { useState, useMemo } from "react";
import type { PolicyEntry } from "@/lib/types";
import { useRelativeTime } from "@/lib/hooks";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { Separator } from "@/components/ui/separator";
import { cn } from "@/lib/utils";

interface PolicyCardProps {
  policy: PolicyEntry;
  onToggle: (policyId: string, enabled: boolean) => void;
  onDelete: (policyId: string) => void;
  onUpdate: (policyId: string, cedarText: string) => void;
  acting: boolean;
}

const SOURCE_BADGES: Record<string, { label: string; className: string }> = {
  "os-app": { label: "Base", className: "bg-[var(--color-accent-teal-dim)] text-[var(--color-accent-teal)] border-transparent" },
  decision: { label: "Approved", className: "bg-purple-500/10 text-purple-400 border-transparent" },
  manual: { label: "Manual", className: "bg-[var(--color-bg-elevated)] text-[var(--color-text-secondary)] border-transparent" },
  "migrated-legacy": { label: "Legacy", className: "bg-amber-500/10 text-amber-400 border-transparent" },
};

function extractPolicySummary(cedarText: string): string {
  const lines = cedarText.trim().split("\n").map((l) => l.trim());
  const parts: string[] = [];
  for (const line of lines) {
    const principalExact = line.match(/principal\s*==\s*(\w+)::"([^"]+)"/);
    if (principalExact) { parts.push(`${principalExact[1]}::${principalExact[2]}`); continue; }
    const principalIs = line.match(/principal\s+is\s+(\w+)/);
    if (principalIs) { parts.push(`any ${principalIs[1]}`); continue; }
    const actionExact = line.match(/action\s*==\s*Action::"([^"]+)"/);
    if (actionExact) { parts.push(actionExact[1]); continue; }
    const resourceIs = line.match(/resource\s+is\s+(\w+)/);
    if (resourceIs) { parts.push(`on ${resourceIs[1]}`); continue; }
    const resourceExact = line.match(/resource\s*==\s*(\w+)::"([^"]+)"/);
    if (resourceExact) { parts.push(`on ${resourceExact[1]}::${resourceExact[2]}`); continue; }
  }
  return parts.length > 0 ? parts.join(" → ") : "";
}

export default function PolicyCard({ policy, onToggle, onDelete, onUpdate, acting }: PolicyCardProps) {
  const [expanded, setExpanded] = useState(false);
  const [editing, setEditing] = useState(false);
  const [editText, setEditText] = useState(policy.cedar_text);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const relativeTime = useRelativeTime(policy.created_at);

  const badge = SOURCE_BADGES[policy.source] || SOURCE_BADGES.manual;
  const summary = useMemo(() => extractPolicySummary(policy.cedar_text), [policy.cedar_text]);
  const isLong = policy.cedar_text.split("\n").length > 5;

  const handleSave = () => { onUpdate(policy.policy_id, editText); setEditing(false); };
  const handleCancel = () => { setEditText(policy.cedar_text); setEditing(false); };

  return (
    <Card className={cn("glass rounded-[2px] border-0 animate-fade-in transition-opacity", !policy.enabled && "opacity-50")}>
      <CardContent className="p-4">
        {/* Header row */}
        <div className="flex items-start justify-between mb-2">
          <div className="flex items-center gap-2 min-w-0">
            <Badge variant="outline" className={cn("text-[10px] font-medium", badge.className)}>
              {badge.label}
            </Badge>
            <span className="text-[12px] font-mono text-[var(--color-text-secondary)] truncate" title={policy.policy_id}>
              {policy.policy_id}
            </span>
          </div>
          <Switch
            checked={policy.enabled}
            disabled={acting}
            onCheckedChange={(checked) => onToggle(policy.policy_id, checked)}
            aria-label={policy.enabled ? "Disable policy" : "Enable policy"}
            className="flex-shrink-0 scale-75"
          />
        </div>

        {summary && (
          <div className="text-[12px] text-[var(--color-text-secondary)] mb-2 font-mono">{summary}</div>
        )}

        {/* Cedar text */}
        {editing ? (
          <div className="space-y-2">
            <textarea
              value={editText}
              onChange={(e) => setEditText(e.target.value)}
              className="w-full min-h-[120px] p-2.5 bg-black/30 rounded-[2px] text-[11px] font-mono text-[var(--color-text-primary)] border border-[var(--color-border)] focus:outline-none focus:border-[var(--color-accent-teal)] resize-y"
              spellCheck={false}
            />
            <div className="flex gap-2">
              <Button size="sm" disabled={acting || editText === policy.cedar_text} onClick={handleSave}
                className="rounded-[2px] h-auto px-2.5 py-1 text-[11px]">Save</Button>
              <Button size="sm" variant="secondary" onClick={handleCancel}
                className="rounded-[2px] h-auto px-2.5 py-1 text-[11px]">Cancel</Button>
            </div>
          </div>
        ) : (
          <div>
            <div onClick={() => isLong && setExpanded(!expanded)} className={isLong ? "cursor-pointer" : undefined}>
              <pre className={cn(
                "p-2 bg-black/30 rounded-[2px] text-[11px] font-mono text-[var(--color-text-secondary)] overflow-x-auto whitespace-pre-wrap border border-[var(--color-border)]",
                !expanded && isLong && "max-h-[80px] overflow-hidden"
              )}>
                {policy.cedar_text}
              </pre>
            </div>
            {isLong && (
              <Button variant="ghost" size="sm" onClick={() => setExpanded(!expanded)}
                className="h-auto text-[10px] text-[var(--color-text-muted)] p-0 mt-1 hover:text-[var(--color-text-secondary)]">
                {expanded ? "Collapse" : "Expand"}
              </Button>
            )}
          </div>
        )}

        {/* Footer */}
        <Separator className="my-2 bg-[var(--color-border)]" />
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3 text-[10px] text-[var(--color-text-muted)]">
            <span>by {policy.created_by}</span>
            {relativeTime && <span>{relativeTime}</span>}
            <span className="font-mono" title={policy.policy_hash}>{policy.policy_hash.slice(0, 8)}</span>
          </div>
          <div className="flex items-center gap-1.5">
            {!editing && (
              <Button variant="ghost" size="sm" disabled={acting}
                onClick={() => { setEditText(policy.cedar_text); setEditing(true); }}
                className="h-auto px-2 py-0.5 text-[10px] rounded-[2px]">Edit</Button>
            )}
            {confirmDelete ? (
              <div className="flex items-center gap-1">
                <span className="text-[10px] text-[var(--color-accent-pink)]">Delete?</span>
                <Button variant="destructive" size="sm" disabled={acting}
                  onClick={() => { onDelete(policy.policy_id); setConfirmDelete(false); }}
                  className="h-auto px-2 py-0.5 text-[10px] rounded-[2px]">Yes</Button>
                <Button variant="ghost" size="sm" onClick={() => setConfirmDelete(false)}
                  className="h-auto px-2 py-0.5 text-[10px] rounded-[2px] text-[var(--color-text-muted)]">No</Button>
              </div>
            ) : (
              <Button variant="ghost" size="sm" disabled={acting} onClick={() => setConfirmDelete(true)}
                className="h-auto px-2 py-0.5 text-[10px] rounded-[2px] text-[var(--color-text-muted)] hover:text-[var(--color-accent-pink)] hover:bg-[var(--color-accent-pink-dim)]">
                Delete
              </Button>
            )}
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
