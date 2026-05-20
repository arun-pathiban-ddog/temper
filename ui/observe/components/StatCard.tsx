"use client";

import { Card, CardContent } from "@/components/ui/card";
import { cn } from "@/lib/utils";

interface StatCardProps {
  label: string;
  value: string | number;
  color?: string;
  className?: string;
  onAnimationEnd?: () => void;
}

export default function StatCard({ label, value, color, className, onAnimationEnd }: StatCardProps) {
  return (
    <Card
      className={cn("glass rounded-[2px] border-0 shadow-none px-3 py-2.5", className)}
      onAnimationEnd={onAnimationEnd}
    >
      <CardContent className="p-0">
        <div className="text-[11px] text-[var(--color-text-secondary)] font-medium">{label}</div>
        <div className={cn("text-xl font-semibold font-mono mt-0.5", color ?? "text-[var(--color-text-primary)]")}>
          {value}
        </div>
      </CardContent>
    </Card>
  );
}
