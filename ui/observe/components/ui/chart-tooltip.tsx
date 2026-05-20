"use client";

import { useRef, useState } from "react";

export interface ChartTooltipState {
  x: number;
  y: number;
  text: string;
}

export function useChartTooltip() {
  const [tooltip, setTooltip] = useState<ChartTooltipState | null>(null);
  const wrapperRef = useRef<HTMLDivElement>(null);

  const showTooltip = (e: React.MouseEvent, text: string) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const wrapper = wrapperRef.current?.getBoundingClientRect();
    if (!wrapper) return;
    setTooltip({
      x: rect.left - wrapper.left + rect.width / 2,
      y: rect.top - wrapper.top,
      text,
    });
  };

  const hideTooltip = () => setTooltip(null);

  return { tooltip, wrapperRef, showTooltip, hideTooltip };
}

export function ChartTooltip({
  tooltip,
  flip = true,
}: {
  tooltip: ChartTooltipState | null;
  flip?: boolean;
}) {
  if (!tooltip) return null;
  return (
    <div
      className="absolute z-50 pointer-events-none px-2 py-1 rounded text-[11px] leading-tight bg-popover text-popover-foreground border border-border shadow-md whitespace-nowrap"
      style={{
        left: tooltip.x,
        top: tooltip.y,
        transform:
          flip && tooltip.y < 30
            ? "translate(-50%, 6px)"
            : "translate(-50%, -100%) translateY(-6px)",
      }}
    >
      {tooltip.text}
    </div>
  );
}
