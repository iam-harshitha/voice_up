import { Link, useSearchParams } from "react-router-dom";
import { api } from "../api/endpoints";
import type { RequestStatus } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import {
  Button,
  Card,
  EmptyState,
  ErrorState,
  LastUpdated,
  LoadingBlock,
  StaleNotice,
  StatusBadge,
  Unavailable,
  cx,
} from "../components/ui";
import { usePolling } from "../hooks/usePolling";
import { formatDateTime, formatRelative, shortId } from "../lib/format";

const PAGE_SIZE = 25;
const FILTERS: { value: RequestStatus | null; label: string }[] = [
  { value: null, label: "All" },
  { value: "pending", label: "Pending" },
  { value: "approved", label: "Approved" },
  { value: "rejected", label: "Rejected" },
  { value: "expired", label: "Expired" },
];

export function RequestsPage() {
  const { can } = useAuth();
  const [params, setParams] = useSearchParams();
  const rawStatus = params.get("status");
  const status = FILTERS.some((f) => f.value === rawStatus) ? (rawStatus as RequestStatus) : null;
  const page = Math.max(0, Number.parseInt(params.get("page") ?? "0", 10) || 0);

  const list = usePolling(
    `requests:${status}:${page}`,
    (signal) => api.listRequests({ status, limit: PAGE_SIZE, offset: page * PAGE_SIZE }, { signal }),
    { intervalMs: 10_000, enabled: can("requests:read") },
  );

  if (!can("requests:read")) return <Unavailable scope="requests:read" what="The request list" />;

  const set = (next: { status?: RequestStatus | null; page?: number }) => {
    const p = new URLSearchParams();
    const s = next.status === undefined ? status : next.status;
    if (s) p.set("status", s);
    const pg = next.page ?? 0;
    if (pg) p.set("page", String(pg));
    setParams(p);
  };

  const data = list.data;
  const pages = data ? Math.max(1, Math.ceil(data.total / PAGE_SIZE)) : 1;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <h1 className="text-xl font-semibold text-slate-900">Approval requests</h1>
          <p className="text-sm text-slate-500">Newest first. Status and evaluation come from the policy engine.</p>
        </div>
        <div className="flex items-center gap-3">
          <LastUpdated at={list.lastUpdated} polling />
          {can("requests:create") && (
            <Link to="/requests/new" className="rounded-md bg-slate-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-700">
              New request
            </Link>
          )}
        </div>
      </div>

      <div role="group" aria-label="Filter by status" className="flex flex-wrap gap-1">
        {FILTERS.map((f) => (
          <button
            key={f.label}
            type="button"
            aria-pressed={status === f.value}
            onClick={() => set({ status: f.value, page: 0 })}
            className={cx(
              "rounded-full px-3 py-1 text-sm",
              status === f.value ? "bg-slate-900 text-white" : "bg-white text-slate-700 ring-1 ring-slate-300 hover:bg-slate-50",
            )}
          >
            {f.label}
          </button>
        ))}
      </div>
      <p className="text-xs text-slate-500">Text search is not offered: the API supports filtering by status only.</p>

      {list.stale && <StaleNotice lastUpdated={list.lastUpdated} onRetry={list.refresh} />}
      <Card>
        {list.error && !data && <ErrorState error={list.error} onRetry={list.refresh} />}
        {!data && list.loading && <LoadingBlock label="Loading requests" />}
        {data && data.items.length === 0 && <EmptyState title="No requests match this filter." />}
        {data && data.items.length > 0 && (
          <div className="overflow-x-auto">
            <table className="min-w-full text-left text-sm">
              <caption className="sr-only">Approval requests</caption>
              <thead className="border-b border-slate-200 text-xs uppercase tracking-wide text-slate-500">
                <tr>
                  <th scope="col" className="py-2 pr-4">Action</th>
                  <th scope="col" className="py-2 pr-4">State</th>
                  <th scope="col" className="py-2 pr-4">Approvals</th>
                  <th scope="col" className="py-2 pr-4">Policy</th>
                  <th scope="col" className="py-2 pr-4">Created</th>
                  <th scope="col" className="py-2">Deadline</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {data.items.map((r) => (
                  <tr key={r.id} className="align-top">
                    <td className="max-w-md py-2 pr-4">
                      <Link to={`/requests/${r.id}`} className="font-medium text-slate-900 hover:underline">
                        {r.action}
                      </Link>
                      <p className="truncate text-xs text-slate-500" title={r.summary}>
                        {r.summary}
                      </p>
                      <p className="font-mono text-xs text-slate-400">{shortId(r.id)}</p>
                    </td>
                    <td className="py-2 pr-4">
                      <StatusBadge status={r.status} evaluation={r.evaluation} />
                      {r.evaluation && r.evaluation.condition_approver_ids.length > 0 && (
                        <p className="mt-1 text-xs text-amber-800">Open condition</p>
                      )}
                    </td>
                    <td className="py-2 pr-4 tabular-nums">
                      {r.evaluation ? `${r.evaluation.approvals} / ${r.evaluation.required_approvals}` : `— / ${r.required_approvals}`}
                    </td>
                    <td className="py-2 pr-4 text-xs text-slate-600">
                      {shortId(r.policy_id)} · v{r.policy_version}
                    </td>
                    <td className="whitespace-nowrap py-2 pr-4 text-xs text-slate-600" title={formatDateTime(r.created_at)}>
                      {formatRelative(r.created_at)}
                    </td>
                    <td className="whitespace-nowrap py-2 text-xs text-slate-600">
                      {r.expires_at ? formatDateTime(r.expires_at) : "None"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {data && data.total > PAGE_SIZE && (
          <nav aria-label="Pagination" className="mt-4 flex items-center justify-between text-sm">
            <span className="text-slate-600">
              Page {page + 1} of {pages} · {data.total} requests
            </span>
            <div className="flex gap-2">
              <Button disabled={page === 0} onClick={() => set({ page: page - 1 })}>
                Previous
              </Button>
              <Button disabled={page + 1 >= pages} onClick={() => set({ page: page + 1 })}>
                Next
              </Button>
            </div>
          </nav>
        )}
      </Card>
    </div>
  );
}
