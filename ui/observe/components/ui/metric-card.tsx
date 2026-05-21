"use client";

import { cn } from "@/lib/utils";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";

interface MetricCardProps {
  label: string;
  value: number | null;
  loading?: boolean;
  icon?: React.ReactNode;
  onClick?: () => void;
  onMouseEnter?: () => void;
  onMouseLeave?: () => void;
  /** Previous period value — enables trend delta display. */
  previousValue?: number | null;
  formatValue?: (n: number) => string;
  /** When true, an increase is shown as red (bad). Default false (increase = green). */
  invertTrend?: boolean;
  /** Applies a subtle highlight (e.g. when coordinated with a chart). */
  highlighted?: boolean;
  /** Tooltip shown on hover. */
  tooltip?: React.ReactNode;
  /**
   * How to render the trend signal.
   * "delta" (default) — colored "+N" / "-N" text after the value.
   * "dot" — small colored dot before the value, hover for the change value.
   */
  trendDisplay?: "delta" | "dot";
}

export function MetricCard({
  label,
  value,
  loading,
  icon,
  onClick,
  onMouseEnter,
  onMouseLeave,
  previousValue,
  formatValue = (n) => n.toLocaleString(),
  invertTrend = false,
  highlighted = false,
  tooltip,
  trendDisplay = "delta",
}: MetricCardProps) {
  const delta =
    value != null && previousValue != null ? value - previousValue : null;
  const trendUp = delta != null && delta > 0;
  const trendNeutral = delta === 0;
  const trendPct =
    value != null && previousValue != null && previousValue !== 0
      ? ((value - previousValue) / previousValue) * 100
      : null;

  const trendTextClass = trendNeutral
    ? "text-muted-foreground"
    : (trendUp ? !invertTrend : invertTrend)
      ? "text-chart-3"
      : "text-destructive";

  const trendDotClass = trendNeutral
    ? "bg-muted-foreground"
    : (trendUp ? !invertTrend : invertTrend)
      ? "bg-chart-3"
      : "bg-destructive";

  const showDot = trendDisplay === "dot" && delta != null && !loading && !icon;

  const dotTooltipText =
    trendPct != null
      ? `${trendPct > 0 ? "+" : ""}${trendPct.toFixed(1)}% vs previous period`
      : delta != null
        ? `${delta > 0 ? "+" : ""}${formatValue(delta)} vs previous period`
        : null;

  const dotElement = showDot ? (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>
          <span
            className={cn(
              "inline-block size-2 rounded-full shrink-0",
              trendDotClass,
            )}
          />
        </TooltipTrigger>
        <TooltipContent side="top">{dotTooltipText}</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  ) : null;

  const inner = (
    <div className="p-3 flex flex-col justify-between gap-2 h-full">
      <span className="text-xs text-secondary-foreground">{label}</span>
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          {icon ? <span className="shrink-0">{icon}</span> : dotElement}
          <span className="font-semibold text-xl">
            {loading ? "--" : value != null ? formatValue(value) : "--"}
          </span>
        </div>
        {trendDisplay === "delta" && delta != null && !loading && (
          <span className={`text-xs font-semibold shrink-0 ${trendTextClass}`}>
            {delta > 0 ? "+" : ""}
            {delta}
          </span>
        )}
      </div>
    </div>
  );

  const cardClass = cn(
    "flex flex-col gap-0 rounded-xl border bg-card text-card-foreground shadow-sm",
    "transition-colors h-full py-0",
    highlighted && "bg-muted/50",
    onClick && "cursor-pointer hover:bg-muted/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
  );

  const card = onClick ? (
    <button
      type="button"
      data-slot="card"
      className={cn(cardClass, "w-full text-left")}
      onClick={onClick}
      onMouseEnter={onMouseEnter}
      onMouseLeave={onMouseLeave}
    >
      {inner}
    </button>
  ) : (
    <div
      data-slot="card"
      className={cardClass}
      onMouseEnter={onMouseEnter}
      onMouseLeave={onMouseLeave}
    >
      {inner}
    </div>
  );

  if (tooltip) {
    return (
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>{card}</TooltipTrigger>
          <TooltipContent side="top">{tooltip}</TooltipContent>
        </Tooltip>
      </TooltipProvider>
    );
  }

  return card;
}
