import type { AuditEvent } from "../api/types";
import { eventLabel, eventTone, formatDateTime } from "../lib/format";
import { EmptyState, cx } from "./ui";

const TONE: Record<ReturnType<typeof eventTone>, string> = {
  good: "bg-emerald-600",
  bad: "bg-red-600",
  warn: "bg-amber-500",
  info: "bg-sky-600",
  other: "bg-slate-400",
};

// Event data keys that refer to approvers: shown by name instead of raw IDs.
const APPROVER_KEYS = new Set(["approver_id", "seat_approver_id", "backup_approver_id", "by_approver_id"]);
// Keys too internal or noisy to show inline.
const HIDDEN_KEYS = new Set(["approve_votes", "reject_votes"]);

function renderValue(key: string, value: unknown, nameOf: (id: string) => string): string {
  if (value === null || value === undefined) return "—";
  if (APPROVER_KEYS.has(key) && typeof value === "string") return nameOf(value);
  if (Array.isArray(value)) {
    if (value.length === 0) return "none";
    return value.map((v) => (key.endsWith("approver_ids") && typeof v === "string" ? nameOf(v) : String(v))).join(", ");
  }
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

/** Persisted audit events in order (by sequence). Unknown event types are shown generically. */
export function Timeline({ events, nameOf }: { events: AuditEvent[]; nameOf: (id: string) => string }) {
  if (events.length === 0) return <EmptyState title="No audit events." />;
  const ordered = [...events].sort((a, b) => a.sequence - b.sequence);
  return (
    <ol className="relative space-y-4 border-l border-slate-200 pl-5">
      {ordered.map((event) => {
        const entries = Object.entries(event.event_data).filter(([k]) => !HIDDEN_KEYS.has(k));
        return (
          <li key={event.sequence} className="relative">
            <span
              aria-hidden="true"
              className={cx("absolute -left-[1.6rem] top-1.5 h-2.5 w-2.5 rounded-full ring-2 ring-white", TONE[eventTone(event.event_type)])}
            />
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <p className="text-sm font-medium text-slate-900">{eventLabel(event.event_type)}</p>
              <time dateTime={event.created_at} className="text-xs text-slate-500">
                {formatDateTime(event.created_at)}
              </time>
            </div>
            <p className="font-mono text-[11px] text-slate-400">
              #{event.sequence} · {event.event_type}
            </p>
            {entries.length > 0 && (
              <details className="mt-1">
                <summary className="cursor-pointer text-xs text-sky-700">Details</summary>
                <dl className="mt-1 grid grid-cols-1 gap-x-4 gap-y-0.5 text-xs sm:grid-cols-2">
                  {entries.map(([key, value]) => (
                    <div key={key} className="flex gap-2">
                      <dt className="text-slate-500">{key.replace(/_/g, " ")}:</dt>
                      <dd className="break-all text-slate-800">{renderValue(key, value, nameOf)}</dd>
                    </div>
                  ))}
                </dl>
              </details>
            )}
          </li>
        );
      })}
    </ol>
  );
}
