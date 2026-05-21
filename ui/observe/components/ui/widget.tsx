"use client";

import { Button } from "@/components/ui/button";
import { CollapsibleTrigger } from "@/components/ui/collapsible";
import {
  ContextualMenu,
  type ContextualMenuProps,
} from "@/components/ui/contextual-menu";
import { cn } from "@/lib/utils";
import { ArrowLeft, ArrowRight, ChevronDown, MoreVertical } from "lucide-react";
import type * as React from "react";
import { useState } from "react";

function Widget({ className, ref, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      ref={ref}
      data-slot="widget"
      className={cn(
        "group/hover-host relative flex flex-col rounded-lg border border-transparent bg-card py-3 text-card-foreground shadow-card transition-colors hover:not-has-[[data-drag-wrapper]:hover]:bg-muted/50",
        className,
      )}
      {...props}
    />
  );
}

function WidgetHeader({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="widget-header"
      className={cn("flex items-center gap-3 px-4", className)}
      {...props}
    />
  );
}

function WidgetTitle({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="widget-title"
      className={cn(
        "flex h-6 items-center gap-2 text-sm font-medium leading-6",
        className,
      )}
      {...props}
    />
  );
}

function WidgetCollapseTrigger({
  className,
  "aria-label": ariaLabel = "Toggle widget",
  ...props
}: Omit<React.ComponentProps<typeof Button>, "size" | "variant" | "asChild">) {
  return (
    <CollapsibleTrigger asChild>
      <Button
        data-slot="widget-collapse-trigger"
        size="icon-xs"
        variant="ghost"
        aria-label={ariaLabel}
        className={cn(
          "group/widget-collapse -ml-1 text-muted-foreground hover:text-foreground",
          className,
        )}
        {...props}
      >
        <ChevronDown className="size-3 shrink-0 transition-transform duration-150 group-data-[state=closed]/widget-collapse:-rotate-90" />
      </Button>
    </CollapsibleTrigger>
  );
}

type WidgetPaginationProps = Omit<React.ComponentProps<"div">, "children"> & {
  page: number;
  total: number;
  onPrevious?: () => void;
  onNext?: () => void;
  previousLabel?: string;
  nextLabel?: string;
};

function WidgetPagination({
  className,
  page,
  total,
  onPrevious,
  onNext,
  previousLabel = "Previous",
  nextLabel = "Next",
  ...props
}: WidgetPaginationProps) {
  if (total <= 0) return null;
  return (
    <div
      data-slot="widget-pagination"
      className={cn(
        "flex items-center gap-2 text-sm text-muted-foreground",
        className,
      )}
      {...props}
    >
      <Button
        size="icon-xs"
        variant="ghost"
        aria-label={previousLabel}
        disabled={page <= 1 || onPrevious == null}
        onClick={onPrevious}
      >
        <ArrowLeft className="size-3 shrink-0" />
      </Button>
      <span aria-live="polite">
        {page} of {total}
      </span>
      <Button
        size="icon-xs"
        variant="ghost"
        aria-label={nextLabel}
        disabled={page >= total || onNext == null}
        onClick={onNext}
      >
        <ArrowRight className="size-3 shrink-0" />
      </Button>
    </div>
  );
}

type WidgetMenuButtonProps = Omit<ContextualMenuProps, "children"> & {
  className?: string;
  "aria-label"?: string;
};

function WidgetMenuButton({
  className,
  "aria-label": ariaLabel = "More actions",
  open: controlledOpen,
  onOpenChange,
  ...menuProps
}: WidgetMenuButtonProps) {
  const [internalOpen, setInternalOpen] = useState(false);
  const isOpen = controlledOpen ?? internalOpen;
  const handleOpenChange = (next: boolean) => {
    setInternalOpen(next);
    onOpenChange?.(next);
  };

  return (
    <ContextualMenu
      {...menuProps}
      open={isOpen}
      onOpenChange={handleOpenChange}
    >
      <Button
        data-slot="widget-menu-button"
        data-state={isOpen ? "open" : "closed"}
        size="icon-xs"
        variant="ghost"
        aria-label={ariaLabel}
        className={cn(
          "text-muted-foreground opacity-0 transition-opacity duration-[120ms] hover:text-foreground",
          "group-hover/hover-host:opacity-100 focus-visible:opacity-100 data-[state=open]:opacity-100",
          className,
        )}
      >
        <MoreVertical className="size-3 shrink-0" />
      </Button>
    </ContextualMenu>
  );
}

type WidgetSeeAllButtonProps = Omit<
  React.ComponentProps<typeof Button>,
  "size" | "variant" | "asChild"
>;

function WidgetSeeAllButton({
  className,
  children = "See all",
  ...props
}: WidgetSeeAllButtonProps) {
  return (
    <Button
      data-slot="widget-see-all"
      size="xs"
      variant="ghost"
      className={cn("text-muted-foreground hover:text-foreground", className)}
      {...props}
    >
      {children}
    </Button>
  );
}

function WidgetActions({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="widget-actions"
      className={cn("ml-auto flex items-center gap-2", className)}
      {...props}
    />
  );
}

function WidgetDescription({ className, ...props }: React.ComponentProps<"p">) {
  return (
    <p
      data-slot="widget-description"
      className={cn(
        "px-4 pt-1 font-mono text-xs text-muted-foreground",
        className,
      )}
      {...props}
    />
  );
}

function WidgetContent({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="widget-content"
      className={cn("px-4 py-3", className)}
      {...props}
    />
  );
}

function WidgetSeparator({ className, ...props }: React.ComponentProps<"hr">) {
  return (
    <hr
      data-slot="widget-separator"
      className={cn(
        "mx-4 my-0 h-0 border-0 border-t border-dashed border-border",
        className,
      )}
      {...props}
    />
  );
}

function WidgetFooter({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="widget-footer"
      className={cn(
        "flex items-center gap-2 px-4 pt-3 text-sm text-foreground",
        className,
      )}
      {...props}
    />
  );
}

export {
  Widget,
  WidgetActions,
  WidgetCollapseTrigger,
  WidgetContent,
  WidgetDescription,
  WidgetFooter,
  WidgetHeader,
  WidgetMenuButton,
  WidgetPagination,
  WidgetSeeAllButton,
  WidgetSeparator,
  WidgetTitle,
};
