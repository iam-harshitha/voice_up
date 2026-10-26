import { Link } from "react-router-dom";
import { api } from "../api/endpoints";
import { useAuth } from "../auth/AuthContext";
import {
  Card,
  EmptyState,
  ErrorState,
  LastUpdated,
  LoadingBlock,
  StaleNotice,
  StatusBadge,
  Unavailable,
} from "../components/ui";
import { usePolling } from "../hooks/usePolling";
import { eventLabel, formatRelative, shortId } from "../lib/format";

const REFRESH_MS = 10_000;

function Metric({ label, value, hint }: { label: string; value: number | string; hint?: string }) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
      <p className="text-xs font-medium uppercase tracking-wide text-slate-500">{label}</p>
      <p className="mt-1 text-2xl font-semibold text-slate-900">{value}</p>
      {hint && <p className="mt-1 text-xs text-slate-500">{hint}</p>}
    </div>
  );
}

export function OverviewPage() {
  const { can } = useAuth();
  const stats = usePolling("stats", (signal) => api.stats({ signal }), {
    intervalMs: REFRESH_MS,
    enabled: can("requests:read"),
  });
  const recent = usePolling(
    "recent-requests",
    (signal) => api.listRequests({ limit: 8, offset: 0 }, { signal }),
    { intervalMs: REFRESH_MS, enabled: can("requests:read") },
  );
  const events = usePolling("recent-events", (signal) => api.recentEvents(12, { signal }), {
    intervalMs: REFRESH_MS,
    enabled: can("audit:read"),
  });

  if (!can("requests:read")) {
    return <Unavailable scope="requests:read" what="The overview" />;
  }

  const s = stats.data;
  const partial =
    s && !s.evaluation_complete
      ? `Computed over the ${s.pending_evaluated} most recent pending requests (of ${s.by_status.pending}).`
      : undefined;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <h1 className="text-xl font-semibold text-slate-900">Overview</h1>
          <p className="text-sm text-slate-500">Live state of approval requests in your organization.</p>
        </div>
        <LastUpdated at={stats.lastUpdated} polling />
      </div>

      {stats.stale && <StaleNotice lastUpdated={stats.lastUpdated} onRetry={stats.refresh} />}
      {stats.error && !s && <ErrorState error={stats.error} onRetry={stats.refresh} />}
      {!s && stats.loading && <LoadingBlock label="Loading metrics" />}
      {s && (
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <Metric label="Total requests" value={s.total} />
          <Metric label="Pending" value={s.by_status.pending} />
          <Metric label="Approved" value={s.by_status.approved} />
          <Metric label="Rejected" value={s.by_status.rejected} />
          <Metric label="Expired" value={s.by_status.expired} />
          <Metric label="Waiting for a condition" value={s.pending_waiting_for_condition} hint={partial} />
          <Metric label="Awaiting re-approval" value={s.pending_awaiting_reapproval} hint={partial} />
          <Metric
            label="Past deadline, expiring"
            value={s.pending_deadline_passed}
            hint={partial ?? "Pending requests whose deadline passed; the worker will expire them."}
          />
        </div>
      )}

      <div className="grid gap-6 lg:grid-cols-2">
        <Card title="Recent requests" actions={<Link className="text-sm text-sky-700 hover:underline" to="/requests">View all</Link>}>
          {recent.error && !recent.data && <ErrorState error={recent.error} onRetry={recent.refresh} compact />}
          {!recent.data && recent.loading && <LoadingBlock />}
          {recent.data && recent.data.items.length === 0 && (
            <EmptyState title="No approval requests yet.">
              Agents create them through the API or MCP, or use <Link className="text-sky-700 underline" to="/requests/new">New request</Link>.
            </EmptyState>
          )}
          {recent.data && recent.data.items.length > 0 && (
            <ul className="divide-y divide-slate-100">
              {recent.data.items.map((r) => (
                <li key={r.id} className="flex items-center justify-between gap-3 py-2">
                  <div className="min-w-0">
                    <Link to={`/requests/${r.id}`} className="block truncate text-sm font-medium text-slate-900 hover:underline">
                      {r.action}
                    </Link>
                    <p className="truncate text-xs text-slate-500">
                      {shortId(r.id)} · {formatRelative(r.created_at)}
                    </p>
                  </div>
                  <StatusBadge status={r.status} evaluation={r.evaluation} />
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card title="Recent workflow events">
          {!can("audit:read") ? (
            <Unavailable scope="audit:read" what="The activity feed" />
          ) : (
            <>
              {events.error && !events.data && <ErrorState error={events.error} onRetry={events.refresh} compact />}
              {!events.data && events.loading && <LoadingBlock />}
              {events.data && events.data.items.length === 0 && <EmptyState title="No events recorded yet." />}
              {events.data && events.data.items.length > 0 && (
                <ul className="divide-y divide-slate-100">
                  {events.data.items.map((e) => (
                    <li key={e.sequence} className="flex items-center justify-between gap-3 py-2 text-sm">
                      <span className="min-w-0 truncate">
                        <span className="font-medium text-slate-900">{eventLabel(e.event_type)}</span>
                        {e.request_id && (
                          <Link to={`/requests/${e.request_id}`} className="ml-2 font-mono text-xs text-sky-700 hover:underline">
                            {shortId(e.request_id)}
                          </Link>
                        )}
                        {e.policy_id && !e.request_id && <span className="ml-2 text-xs text-slate-500">policy</span>}
                      </span>
                      <span className="whitespace-nowrap text-xs text-slate-500">{formatRelative(e.created_at)}</span>
                    </li>
                  ))}
                </ul>
              )}
            </>
          )}
        </Card>
      </div>
    </div>
  );
}
