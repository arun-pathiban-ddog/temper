"use client";

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { ChevronDown } from "lucide-react";
import { useEffect, useState } from "react";
import { createPortal } from "react-dom";

// ── MenuItem type system ───────────────────────────────────────────────────

export type MenuActionItem = {
  type: "action";
  content: React.ReactNode;
  /**
   * When true, selecting this item does not close the menu.
   * Use for in-menu actions like copy-to-clipboard.
   */
  preventClose?: boolean;
  onActivate?: () => void;
};

export type MenuSeparatorItem = {
  type: "separator";
};

export type MenuSubmenuItem = {
  type: "submenu";
  label: string;
  icon?: React.ReactNode;
  children: Array<MenuActionItem | MenuSeparatorItem>;
};

export type MenuItem = MenuActionItem | MenuSeparatorItem | MenuSubmenuItem;

// ── Props ──────────────────────────────────────────────────────────────────

export interface ContextualMenuProps {
  items?: MenuItem[];
  /** Trigger element — wrapped via DropdownMenuTrigger asChild. */
  children: React.ReactNode;
  /**
   * Title rendered in the menu header — typically the entity the row represents.
   */
  title?: string;
  /**
   * Inline accessory rendered on the right side of the menu header.
   */
  headerAccessory?: React.ReactNode;
  /**
   * Optional content rendered inside the dropdown content, below the items.
   */
  footer?: React.ReactNode;
  /** Optional controlled open state. */
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
}

const ACTIONS_VISIBLE_THRESHOLD = 5;

function renderItem(item: MenuItem, key: number): React.ReactNode {
  if (item.type === "separator") {
    return <DropdownMenuSeparator key={key} className="mx-3" />;
  }
  if (item.type === "submenu") {
    return (
      <DropdownMenuSub key={key}>
        <DropdownMenuSubTrigger className="px-3 py-2 text-[13px] leading-[1.5]">
          {item.icon && <span className="shrink-0">{item.icon}</span>}
          <span>{item.label}</span>
        </DropdownMenuSubTrigger>
        <DropdownMenuSubContent>
          {item.children.map((child, i) => renderItem(child, i))}
        </DropdownMenuSubContent>
      </DropdownMenuSub>
    );
  }
  const actionItem = item as MenuActionItem;
  return (
    <DropdownMenuItem
      key={key}
      onSelect={(e) => {
        if (actionItem.preventClose) e.preventDefault();
        actionItem.onActivate?.();
      }}
      className="px-3 py-2 text-sm leading-[1.5] rounded-none [&_a]:block [&_a]:w-full [&_a]:text-foreground [&_a]:no-underline"
    >
      {item.content}
    </DropdownMenuItem>
  );
}

// ── MenuCard ────────────────────────────────────────────────────────────────

interface MenuCardProps {
  items: MenuItem[];
  title: string | undefined;
  headerAccessory: React.ReactNode;
  actionsExpanded: boolean;
  onShowMore: () => void;
}

function MenuCard({
  items,
  title,
  headerAccessory,
  actionsExpanded,
  onShowMore,
}: MenuCardProps) {
  const actionableCount = items.filter((i) => i.type !== "separator").length;
  if (actionableCount === 0) return null;

  const scrollable = actionsExpanded && actionableCount > ACTIONS_VISIBLE_THRESHOLD;
  const visibleItems = actionsExpanded ? items : items.slice(0, ACTIONS_VISIBLE_THRESHOLD);
  const hiddenCount = actionableCount - ACTIONS_VISIBLE_THRESHOLD;
  const hasHeader = !!title || !!headerAccessory;

  return (
    <div className="flex flex-col rounded-lg border border-border bg-card overflow-hidden">
      {hasHeader && (
        <div className="flex items-center justify-between gap-3 px-3 pt-2.5 pb-1.5">
          {title && (
            <span className="text-[11px] font-semibold text-muted-foreground truncate">
              {title}
            </span>
          )}
          {headerAccessory && (
            <div className="ml-auto shrink-0">{headerAccessory}</div>
          )}
        </div>
      )}
      <div className={scrollable ? "max-h-[200px] overflow-y-auto" : undefined}>
        {visibleItems.map((item, i) => renderItem(item, i))}
      </div>
      {!actionsExpanded && hiddenCount > 0 && (
        <DropdownMenuItem
          className="w-full text-center text-[12px] text-primary py-2 px-3 justify-center rounded-none bg-card hover:bg-accent"
          onSelect={(e) => {
            e.preventDefault();
            onShowMore();
          }}
        >
          Show more{" "}
          <ChevronDown size={11} className="inline-block align-middle ml-1" />
        </DropdownMenuItem>
      )}
    </div>
  );
}

// ── ContextualMenu ─────────────────────────────────────────────────────────

export function ContextualMenu({
  items = [],
  children,
  title,
  headerAccessory,
  footer,
  open: controlledOpen,
  onOpenChange,
}: ContextualMenuProps) {
  const actionableCount = items.filter((i) => i.type !== "separator").length;
  const [internalOpen, setInternalOpen] = useState(false);
  const [cursorPos, setCursorPos] = useState({ x: 0, y: 0 });
  const [collisionBoundary, setCollisionBoundary] =
    useState<HTMLElement | null>(null);
  const [actionsExpanded, setActionsExpanded] = useState(
    actionableCount <= ACTIONS_VISIBLE_THRESHOLD,
  );

  const isOpen = controlledOpen ?? internalOpen;

  function handleOpenChange(next: boolean) {
    setInternalOpen(next);
    onOpenChange?.(next);
  }

  useEffect(() => {
    if (isOpen)
      setActionsExpanded(actionableCount <= ACTIONS_VISIBLE_THRESHOLD);
  }, [isOpen, actionableCount]);

  function handlePointerDown(e: React.PointerEvent) {
    if (e.button !== 0 || e.ctrlKey) return;
    const target = e.target as Element | null;
    const nearestDialogContent = target?.closest(
      "[data-slot='dialog-content']",
    );
    const dialogEl =
      nearestDialogContent instanceof HTMLElement ? nearestDialogContent : null;
    setCollisionBoundary(dialogEl);
    if (dialogEl) {
      const rect = dialogEl.getBoundingClientRect();
      setCursorPos({ x: e.clientX - rect.left, y: e.clientY - rect.top });
    } else {
      setCursorPos({ x: e.clientX, y: e.clientY });
    }
    setInternalOpen(true);
    onOpenChange?.(true);
  }

  const inDialog = collisionBoundary != null;

  return (
    <>
      <div style={{ display: "contents" }} onPointerDown={handlePointerDown}>
        {children}
      </div>
      {typeof document !== "undefined" &&
        createPortal(
          <DropdownMenu open={isOpen} onOpenChange={handleOpenChange}>
            <DropdownMenuTrigger asChild>
              <span
                aria-hidden
                style={{
                  position: "fixed",
                  top: cursorPos.y,
                  left: cursorPos.x,
                  width: 0,
                  height: 0,
                  overflow: "hidden",
                  pointerEvents: "none",
                }}
              />
            </DropdownMenuTrigger>
            <DropdownMenuContent
              data-contextual-menu
              align="start"
              sideOffset={4}
              collisionBoundary={inDialog ? collisionBoundary : undefined}
              collisionPadding={inDialog ? 8 : undefined}
              sticky={inDialog ? "always" : undefined}
              className="min-w-[240px] p-0 rounded-lg overflow-hidden"
            >
              <MenuCard
                items={items}
                title={title}
                headerAccessory={headerAccessory}
                actionsExpanded={actionsExpanded}
                onShowMore={() => setActionsExpanded(true)}
              />
              {footer ? (
                <div className="border-t border-border px-3 py-2">{footer}</div>
              ) : null}
            </DropdownMenuContent>
          </DropdownMenu>,
          document.body,
        )}
    </>
  );
}
