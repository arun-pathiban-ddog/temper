"use client";

import { useCallback, useMemo, useRef } from "react";
import type { SpecSummary } from "@/lib/types";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import { Card } from "@/components/ui/card";
import { cn } from "@/lib/utils";

export interface PermissionsSelection {
  entities: Map<string, { allActions: boolean; actions: Set<string> }>;
}

export function emptySelection(): PermissionsSelection {
  return { entities: new Map() };
}

export function countPolicies(selection: PermissionsSelection): number {
  let count = 0;
  for (const [, entry] of selection.entities) {
    if (entry.allActions) count += 1;
    else if (entry.actions.size > 0) count += entry.actions.size;
  }
  return count;
}

interface PermissionsMatrixProps {
  specs: SpecSummary[];
  tenant: string;
  value: PermissionsSelection;
  onChange: (value: PermissionsSelection) => void;
}

function IndeterminateCheckbox({
  checked,
  indeterminate,
  onCheckedChange,
  id,
}: {
  checked: boolean;
  indeterminate: boolean;
  onCheckedChange: () => void;
  id?: string;
}) {
  const ref = useRef<HTMLButtonElement>(null);

  return (
    <Checkbox
      ref={ref}
      id={id}
      checked={indeterminate ? "indeterminate" : checked}
      onCheckedChange={onCheckedChange}
      className="rounded-[2px] border-[var(--color-border)] flex-shrink-0"
    />
  );
}

export default function PermissionsMatrix({ specs, tenant, value, onChange }: PermissionsMatrixProps) {
  const entityGroups = useMemo(() => {
    const groups = new Map<string, string[]>();
    for (const s of specs) {
      if (s.tenant !== tenant) continue;
      const existing = groups.get(s.entity_type);
      if (existing) {
        for (const a of s.actions) {
          if (!existing.includes(a)) existing.push(a);
        }
      } else {
        groups.set(s.entity_type, [...s.actions]);
      }
    }
    for (const [, actions] of groups) actions.sort();
    return groups;
  }, [specs, tenant]);

  const toggleEntityType = useCallback(
    (entityType: string) => {
      const next = new Map(value.entities);
      const current = next.get(entityType);
      if (current?.allActions) {
        next.delete(entityType);
      } else {
        next.set(entityType, { allActions: true, actions: new Set() });
      }
      onChange({ entities: next });
    },
    [value, onChange],
  );

  const toggleAction = useCallback(
    (entityType: string, action: string) => {
      const next = new Map(value.entities);
      const current = next.get(entityType) || { allActions: false, actions: new Set<string>() };
      const nextActions = new Set(current.actions);

      if (current.allActions) {
        const allActions = entityGroups.get(entityType) || [];
        for (const a of allActions) {
          if (a !== action) nextActions.add(a);
        }
        next.set(entityType, { allActions: false, actions: nextActions });
      } else if (nextActions.has(action)) {
        nextActions.delete(action);
        if (nextActions.size === 0) next.delete(entityType);
        else next.set(entityType, { allActions: false, actions: nextActions });
      } else {
        nextActions.add(action);
        const allActions = entityGroups.get(entityType) || [];
        if (nextActions.size === allActions.length) {
          next.set(entityType, { allActions: true, actions: new Set() });
        } else {
          next.set(entityType, { allActions: false, actions: nextActions });
        }
      }
      onChange({ entities: next });
    },
    [value, onChange, entityGroups],
  );

  if (entityGroups.size === 0) {
    return <div className="text-xs text-[var(--color-text-muted)] py-2">No entity types found for this tenant.</div>;
  }

  return (
    <div className="space-y-1">
      <div className="text-[10px] text-[var(--color-text-secondary)] uppercase tracking-wider font-medium mb-1.5">
        Permissions
      </div>
      <Card className="rounded-[2px] border-[var(--color-border)] gap-0 divide-y divide-[var(--color-border)] overflow-hidden">
        {Array.from(entityGroups.entries()).map(([entityType, actions]) => {
          const entry = value.entities.get(entityType);
          const isAllActions = entry?.allActions ?? false;
          const checkedActions = entry?.actions ?? new Set<string>();
          const someChecked = checkedActions.size > 0 || isAllActions;
          const isIndeterminate = !isAllActions && checkedActions.size > 0;
          const entityId = `entity-${entityType}`;

          return (
            <div key={entityType} className="px-3 py-2">
              <div className="flex items-center gap-2">
                <IndeterminateCheckbox
                  id={entityId}
                  checked={isAllActions}
                  indeterminate={isIndeterminate}
                  onCheckedChange={() => toggleEntityType(entityType)}
                />
                <Label htmlFor={entityId} className="font-mono text-[12px] text-[var(--color-text-primary)] font-medium cursor-pointer">
                  {entityType}
                </Label>
                {isAllActions && (
                  <span className="text-[10px] text-[var(--color-accent-teal)]">(all actions)</span>
                )}
                {isIndeterminate && (
                  <span className="text-[10px] text-[var(--color-text-muted)]">
                    ({checkedActions.size} of {actions.length})
                  </span>
                )}
              </div>

              {someChecked && (
                <div className="ml-6 mt-1.5 flex flex-wrap gap-x-4 gap-y-1">
                  {actions.map((action) => {
                    const checked = isAllActions || checkedActions.has(action);
                    const actionId = `action-${entityType}-${action}`;
                    return (
                      <div key={action} className="flex items-center gap-1.5">
                        <Checkbox
                          id={actionId}
                          checked={checked}
                          onCheckedChange={() => toggleAction(entityType, action)}
                          className="rounded-[2px] border-[var(--color-border)] flex-shrink-0"
                        />
                        <Label
                          htmlFor={actionId}
                          className={cn("font-mono text-[11px] cursor-pointer", checked ? "text-[var(--color-text-primary)]" : "text-[var(--color-text-muted)]")}
                        >
                          {action}
                        </Label>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          );
        })}
      </Card>
    </div>
  );
}
