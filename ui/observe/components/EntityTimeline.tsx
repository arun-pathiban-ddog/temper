import type { EntityEvent } from "@/lib/types";
import { Card, CardContent } from "@/components/ui/card";

interface EntityTimelineProps {
  events: EntityEvent[];
}

export default function EntityTimeline({ events }: EntityTimelineProps) {
  if (events.length === 0) {
    return (
      <Card className="rounded-[2px] border-[var(--color-border)] gap-0"><CardContent className="p-6 text-center">
        <p className="text-[13px] text-[var(--color-text-secondary)]">No events recorded yet.</p>
      </CardContent></Card>
    );
  }

  return (
    <div className="relative">
      {/* Timeline line */}
      <div className="absolute left-3.5 top-2 bottom-2 w-px bg-[var(--color-border)]" />

      <div className="space-y-0">
        {events.map((event, i) => {
          const isFirst = i === 0;
          const isLast = i === events.length - 1;
          const date = new Date(event.timestamp);
          const timeStr = date.toLocaleTimeString("en-US", {
            hour: "2-digit",
            minute: "2-digit",
            second: "2-digit",
            hour12: false,
          });
          const dateStr = date.toLocaleDateString("en-US", {
            month: "short",
            day: "numeric",
          });

          return (
            <div key={i} className="relative flex gap-3 py-2">
              {/* Timeline dot */}
              <div className="relative z-10 flex-shrink-0">
                <div
                  className={`w-7 h-7 rounded-full flex items-center justify-center ${
                    isFirst
                      ? "bg-[var(--color-accent-teal-dim)]"
                      : isLast
                      ? "bg-[var(--color-accent-lime-dim)]"
                      : "bg-[var(--color-bg-surface)]"
                  }`}
                >
                  <div
                    className={`w-1.5 h-1.5 rounded-full ${
                      isFirst
                        ? "bg-[var(--color-accent-teal)]"
                        : isLast
                        ? "bg-[var(--color-accent-lime)]"
                        : "bg-[var(--color-text-muted)]"
                    }`}
                  />
                </div>
              </div>

              {/* Event content */}
              <Card className="flex-1 rounded-[2px] border-[var(--color-border)] gap-0 min-w-0"><CardContent className="p-2.5">
                <div className="flex items-center justify-between mb-0.5">
                  <span className="font-mono text-[13px] text-[var(--color-accent-teal)] font-medium">
                    {event.action}
                  </span>
                  <span className="text-[10px] text-[var(--color-text-muted)] font-mono">
                    {dateStr} {timeStr}
                  </span>
                </div>
                <div className="flex items-center gap-1.5 text-[13px]">
                  <span className="font-mono text-[var(--color-text-secondary)]">{event.from_state}</span>
                  <span className="text-[var(--color-text-muted)]">{"->"}</span>
                  <span className="font-mono text-[var(--color-text-secondary)]">{event.to_state}</span>
                </div>
                <div className="text-[11px] text-[var(--color-text-muted)] mt-0.5">
                  by {event.actor}
                </div>
              </CardContent></Card>
            </div>
          );
        })}
      </div>
    </div>
  );
}
