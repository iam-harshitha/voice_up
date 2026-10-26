import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import { Link, useParams } from "react-router-dom";
import { ApiError } from "../api/client";
import { api } from "../api/endpoints";
import type { AuditEvent, Condition, RequestDetail, VoiceCall } from "../api/types";
import { useAuth, useSession } from "../auth/AuthContext";
import { VoiceModeBadge } from "../components/Layout";
import { Timeline } from "../components/Timeline";
import {
  AuthorizationBanner,
  Button,
  Card,
  ConfirmDialog,
  EmptyState,
  ErrorState,
  Field,
  LastUpdated,
  LoadingBlock,
  StaleNotice,
  StatusBadge,
  Unavailable,
  inputClass,
  useToast,
} from "../components/ui";
import { usePolling, type PollState } from "../hooks/usePolling";
import {
  callStatusLabel,
  decisionStatusLabel,
  formatDateTime,
  formatDuration,
  formatRelative,
  nextActionLabel,
  reasonLabel,
  stateLabel,
} from "../lib/format";

const ACTIVE_REFRESH_MS = 4_000;
const MAX_TIMELINE_PAGES = 5;

export function RequestDetailPage() {
  const { requestId = "" } = useParams();
  const { can } = useAuth();
  // Refresh only while the request can still change; a final request is loaded once more
  // when it becomes final and then left alone.
  const [live, setLive] = useState(true);
  const interval = live ? ACTIVE_REFRESH_MS : null;
  const detail = usePolling(`request:${requestId}`, (signal) => api.getRequest(requestId, { signal }), {
    intervalMs: interval,
  });
  const request = detail.data;
  useEffect(() => {
    if (request) setLive(request.status === "pending");
  }, [request]);

  const conditions = usePolling(
    `conditions:${requestId}`,
    (signal) => api.listConditions(requestId, { signal }),
    { intervalMs: interval, enabled: can("requests:read") },
  );
  const escalations = usePolling(
    `escalations:${requestId}`,
    (signal) => api.listEscalations(requestId, { signal }),
    { intervalMs: interval, enabled: can("requests:read") },
  );
  const calls = usePolling(
    `calls:${requestId}`,
    (signal) => api.listVoiceCalls(requestId, { signal }).then((r) => r.calls),
    { intervalMs: interval, enabled: can("voice:calls:read") },
  );
  const timeline = usePolling(
    `timeline:${requestId}`,
    async (signal) => {
      const items: AuditEvent[] = [];
      let after = 0;
      for (let page = 0; page < MAX_TIMELINE_PAGES; page += 1) {
        const result = await api.requestTimeline(requestId, after, { signal });
        items.push(...result.items);
        if (result.next_after === null) return { items, truncated: false };
        after = result.next_after;
      }
      return { items, truncated: true };
    },
    { intervalMs: interval, enabled: can("audit:read") },
  );

  const refreshAll = () => {
    detail.refresh();
    conditions.refresh();
    escalations.refresh();
    calls.refresh();
    timeline.refresh();
  };

  if (detail.error && !request) {
    return (
      <div className="space-y-4">
        <Link to="/requests" className="text-sm text-sky-700 hover:underline">
          ← Approval requests
        </Link>
        <ErrorState error={detail.error} onRetry={detail.refresh} />
      </div>
    );
  }
  if (!request) return <LoadingBlock label="Loading request" />;

  const names = new Map(request.approvers.map((a) => [a.approver_id, a.name]));
  const nameOf = (id: string) => names.get(id) ?? `${id.slice(0, 8)}…`;

  return (
    <div className="space-y-5">
      <Link to="/requests" className="text-sm text-sky-700 hover:underline">
        ← Approval requests
      </Link>

      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="text-xl font-semibold text-slate-900">{request.action}</h1>
            <StatusBadge status={request.status} evaluation={request.evaluation} />
          </div>
          <p className="mt-1 max-w-3xl whitespace-pre-wrap text-sm text-slate-700">{request.summary}</p>
          <p className="mt-1 font-mono text-xs text-slate-500">Request {request.id}</p>
        </div>
        <LastUpdated at={detail.lastUpdated} polling={live} />
      </div>
      {detail.stale && <StaleNotice lastUpdated={detail.lastUpdated} onRetry={detail.refresh} />}

      <AuthorizationBanner status={request.status} evaluation={request.evaluation} />

      <div className="grid gap-5 lg:grid-cols-3">
        <div className="space-y-5 lg:col-span-2">
          <EvaluationCard request={request} nameOf={nameOf} />
          <SeatsCard request={request} nameOf={nameOf} />
          <Card title="Conditions">
            {!can("requests:read") ? (
              <Unavailable scope="requests:read" what="Conditions" />
            ) : conditions.error && !conditions.data ? (
              <ErrorState error={conditions.error} onRetry={conditions.refresh} compact />
            ) : !conditions.data ? (
              <LoadingBlock />
            ) : conditions.data.length === 0 ? (
              <EmptyState title="No conditions on this request." />
            ) : (
              <ul className="space-y-3">
                {conditions.data.map((c) => (
                  <ConditionItem
                    key={c.id}
                    condition={c}
                    requestId={request.id}
                    requestPending={request.status === "pending"}
                    nameOf={nameOf}
                    onResolved={refreshAll}
                  />
                ))}
              </ul>
            )}
          </Card>
          <VoiceCard request={request} calls={calls} nameOf={nameOf} onScheduled={refreshAll} />
          <Card title="Audit timeline">
            {!can("audit:read") ? (
              <Unavailable scope="audit:read" what="The audit timeline" />
            ) : timeline.error && !timeline.data ? (
              <ErrorState error={timeline.error} onRetry={timeline.refresh} compact />
            ) : !timeline.data ? (
              <LoadingBlock />
            ) : (
              <>
                {timeline.stale && <StaleNotice lastUpdated={timeline.lastUpdated} onRetry={timeline.refresh} />}
                <Timeline events={timeline.data.items} nameOf={nameOf} />
                {timeline.data.truncated && (
                  <p className="mt-2 text-xs text-slate-500">
                    Showing the first {timeline.data.items.length} events.
                  </p>
                )}
              </>
            )}
          </Card>
        </div>

        <div className="space-y-5">
          <PolicyCard request={request} />
          <Card title="Escalation">
            {escalations.error && !escalations.data ? (
              <ErrorState error={escalations.error} onRetry={escalations.refresh} compact />
            ) : !escalations.data ? (
              <LoadingBlock />
            ) : escalations.data.length === 0 ? (
              <p className="text-sm text-slate-600">
                {request.policy?.escalation_after_seconds
                  ? `No escalation yet. Undecided seats escalate every ${formatDuration(request.policy.escalation_after_seconds)}.`
                  : "This request's policy has no escalation."}
              </p>
            ) : (
              <ol className="space-y-2 text-sm">
                {escalations.data.map((step) => (
                  <li key={step.id} className="rounded-md border border-slate-200 p-2">
                    <p className="font-medium text-slate-900">
                      Step {step.sequence} (round {step.round})
                    </p>
                    <p className="text-slate-700">
                      {nameOf(step.backup_approver_id)} may now decide {nameOf(step.seat_approver_id)}'s seat.
                    </p>
                    <p className="text-xs text-slate-500">
                      {step.outcome.replace(/_/g, " ")} · {formatRelative(step.created_at)}
                    </p>
                  </li>
                ))}
              </ol>
            )}
          </Card>
          <Card title="Request context">
            {Object.keys(request.context).length === 0 ? (
              <p className="text-sm text-slate-500">No context was supplied.</p>
            ) : (
              <pre className="max-h-72 overflow-auto rounded bg-slate-50 p-2 text-xs text-slate-800">
                {JSON.stringify(request.context, null, 2)}
              </pre>
            )}
            <p className="mt-2 text-xs text-slate-500">Supplied by the requesting agent; not verified by Approval Line.</p>
          </Card>
        </div>
      </div>
    </div>
  );
}

function EvaluationCard({ request, nameOf }: { request: RequestDetail; nameOf: (id: string) => string }) {
  const e = request.evaluation;
  if (!e) return <Card title="Policy evaluation"><p className="text-sm text-slate-500">No evaluation returned.</p></Card>;
  const rows: [string, ReactNode][] = [
    ["State", stateLabel(e.state)],
    ["Reason", reasonLabel(e.reason)],
    ["Authorized", e.authorized ? "Yes" : "No"],
    ["Next action", nextActionLabel(e.next_action)],
    ["Approvals", `${e.approvals} of ${e.required_approvals} required`],
    ["Rejections", `${e.rejections}${e.veto_triggered ? " (veto)" : ""}`],
    [
      "Deadline",
      request.expires_at
        ? `${formatDateTime(request.expires_at)} (${formatRelative(request.expires_at)})${e.deadline_passed ? " — passed" : ""}`
        : "None",
    ],
  ];
  if (request.decided_at) rows.push(["Decided", formatDateTime(request.decided_at)]);
  return (
    <Card title="Policy evaluation (authoritative, from the backend)">
      <dl className="grid grid-cols-1 gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
        {rows.map(([label, value]) => (
          <div key={label}>
            <dt className="text-xs font-medium uppercase tracking-wide text-slate-500">{label}</dt>
            <dd className="text-slate-900">{value}</dd>
          </div>
        ))}
      </dl>
      {e.outstanding_approver_ids.length > 0 && (
        <p className="mt-3 text-sm text-slate-700">
          <span className="font-medium">Waiting on:</span> {e.outstanding_approver_ids.map(nameOf).join(", ")}
        </p>
      )}
      {e.condition_approver_ids.length > 0 && (
        <p className="mt-2 text-sm text-amber-900">
          <span className="font-medium">Open condition on seat of:</span> {e.condition_approver_ids.map(nameOf).join(", ")}
        </p>
      )}
      {e.reapproval_approver_ids.length > 0 && (
        <p className="mt-2 text-sm text-amber-900">
          <span className="font-medium">Must re-approve unconditionally:</span>{" "}
          {e.reapproval_approver_ids.map(nameOf).join(", ")}
        </p>
      )}
    </Card>
  );
}

function SeatsCard({ request, nameOf }: { request: RequestDetail; nameOf: (id: string) => string }) {
  const bySeat = new Map(request.votes.map((v) => [v.seat_approver_id, v]));
  const primaries = request.approvers.filter((a) => a.role === "primary");
  const backups = request.approvers.filter((a) => a.role === "backup");
  return (
    <Card title="Approvers and recorded votes">
      <p className="mb-3 text-xs text-slate-500">
        An individual approval does not authorize the request; only the evaluation above does.
      </p>
      <div className="overflow-x-auto">
        <table className="min-w-full text-left text-sm">
          <caption className="sr-only">Seats and votes</caption>
          <thead className="border-b border-slate-200 text-xs uppercase tracking-wide text-slate-500">
            <tr>
              <th scope="col" className="py-2 pr-4">Seat</th>
              <th scope="col" className="py-2 pr-4">Veto</th>
              <th scope="col" className="py-2 pr-4">Vote</th>
              <th scope="col" className="py-2">Recorded</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {primaries.map((seat) => {
              const vote = bySeat.get(seat.approver_id);
              return (
                <tr key={seat.approver_id}>
                  <td className="py-2 pr-4 font-medium text-slate-900">{seat.name}</td>
                  <td className="py-2 pr-4 text-slate-600">{seat.can_veto ? "Veto holder" : "—"}</td>
                  <td className="py-2 pr-4">
                    {vote ? (
                      <span className={vote.vote === "approve" ? "text-emerald-800" : "text-red-800"}>
                        {vote.vote === "approve" ? "✓ Approved" : "✕ Rejected"}
                        {vote.approver_id !== seat.approver_id && ` (by backup ${nameOf(vote.approver_id)})`}
                      </span>
                    ) : (
                      <span className="text-slate-500">No vote</span>
                    )}
                  </td>
                  <td className="py-2 text-xs text-slate-500">{vote ? formatDateTime(vote.created_at) : "—"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {backups.length > 0 && (
        <p className="mt-3 text-sm text-slate-600">
          <span className="font-medium">Backups (escalation order):</span> {backups.map((b) => b.name).join(", ")}
        </p>
      )}
    </Card>
  );
}

function PolicyCard({ request }: { request: RequestDetail }) {
  const p = request.policy;
  return (
    <Card title="Policy snapshot (frozen at creation)">
      {!p ? (
        <p className="text-sm text-slate-500">Not available.</p>
      ) : (
        <dl className="space-y-2 text-sm">
          {[
            ["Policy", `${p.name} (v${p.version})`],
            ["Mode", p.approval_mode === "quorum" ? `Quorum: ${p.required_approvals} of ${request.approvers.filter((a) => a.role === "primary").length}` : `Unanimous (${p.required_approvals})`],
            ["Veto", p.veto_mode.replace(/_/g, " ")],
            ["Deadline", formatDuration(p.deadline_seconds)],
            [
              "Escalation",
              p.escalation_after_seconds
                ? `Every ${formatDuration(p.escalation_after_seconds)}, up to ${p.max_escalations} step(s)`
                : "None",
            ],
          ].map(([label, value]) => (
            <div key={label} className="flex justify-between gap-3">
              <dt className="text-slate-500">{label}</dt>
              <dd className="text-right text-slate-900">{value}</dd>
            </div>
          ))}
        </dl>
      )}
      <p className="mt-3 text-xs text-slate-500">Later policy edits never change this request.</p>
    </Card>
  );
}

function ConditionItem({
  condition,
  requestId,
  requestPending,
  nameOf,
  onResolved,
}: {
  condition: Condition;
  requestId: string;
  requestPending: boolean;
  nameOf: (id: string) => string;
  onResolved: () => void;
}) {
  const { can } = useAuth();
  const toast = useToast();
  const [outcome, setOutcome] = useState<"satisfied" | "not_satisfied">("satisfied");
  const [note, setNote] = useState("");
  const [evidence, setEvidence] = useState("");
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const open = condition.status === "open";

  function review(event: FormEvent) {
    event.preventDefault();
    if (!note.trim()) {
      setError("A resolution note is required.");
      return;
    }
    setError(null);
    setConfirming(true);
  }

  async function resolve() {
    if (busy) return;
    setBusy(true);
    try {
      const result = await api.resolveCondition(requestId, condition.id, {
        outcome,
        note: note.trim(),
        evidence_reference: evidence.trim() || null,
      });
      toast(
        "success",
        outcome === "satisfied"
          ? `Condition marked satisfied. The request is still not authorized until ${nameOf(condition.approver_id)} re-approves.`
          : "Condition marked not satisfied; the seat is undecided again.",
      );
      setConfirming(false);
      if (result.request.status !== "pending") toast("info", `Request is now ${result.request.status}.`);
      onResolved();
    } catch (caught) {
      setConfirming(false);
      setError(caught instanceof ApiError ? caught.userMessage : "Resolution failed.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <li className="rounded-md border border-slate-200 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm font-medium text-slate-900">
          {nameOf(condition.approver_id)}
          {condition.approver_id !== condition.seat_approver_id && ` (for ${nameOf(condition.seat_approver_id)}'s seat)`}
        </p>
        <span className="rounded bg-slate-100 px-2 py-0.5 text-xs font-medium text-slate-700">
          {condition.status.replace(/_/g, " ")}
        </span>
      </div>
      <blockquote className="mt-1 border-l-2 border-amber-300 pl-2 text-sm text-slate-800">“{condition.description}”</blockquote>
      <p className="mt-1 text-xs text-slate-500">
        Via {condition.source} · {formatRelative(condition.created_at)}
        {condition.closed_at && ` · closed ${formatRelative(condition.closed_at)}`}
      </p>
      {condition.resolution_note && <p className="mt-1 text-xs text-slate-600">Resolution note: {condition.resolution_note}</p>}
      {condition.evidence_reference && <p className="text-xs text-slate-600">Evidence: {condition.evidence_reference}</p>}
      {condition.status === "satisfied" && (
        <p className="mt-1 text-xs text-amber-800">Satisfied does not approve: the approver must re-approve unconditionally.</p>
      )}

      {open && requestPending && (
        can("conditions:resolve") ? (
          <form onSubmit={review} className="mt-3 space-y-3 rounded-md bg-slate-50 p-3">
            <fieldset>
              <legend className="text-sm font-medium text-slate-800">Resolve this condition</legend>
              <div className="mt-1 flex gap-4 text-sm">
                {(["satisfied", "not_satisfied"] as const).map((value) => (
                  <label key={value} className="flex items-center gap-1">
                    <input type="radio" name={`outcome-${condition.id}`} checked={outcome === value} onChange={() => setOutcome(value)} />
                    {value === "satisfied" ? "Satisfied" : "Not satisfied"}
                  </label>
                ))}
              </div>
            </fieldset>
            <Field label="Note" error={error}>
              {(props) => (
                <textarea {...props} rows={2} maxLength={1000} value={note} onChange={(e) => setNote(e.target.value)} className={inputClass} />
              )}
            </Field>
            <Field label="Evidence reference (optional)" hint="Ticket, change ID or URL. Stored, not interpreted.">
              {(props) => (
                <input {...props} maxLength={500} value={evidence} onChange={(e) => setEvidence(e.target.value)} className={inputClass} />
              )}
            </Field>
            <Button type="submit" variant="primary" disabled={busy}>
              Review resolution
            </Button>
          </form>
        ) : (
          <div className="mt-2"><Unavailable scope="conditions:resolve" what="Resolving conditions" /></div>
        )
      )}
      <ConfirmDialog
        open={confirming}
        title="Record this resolution?"
        confirmLabel={outcome === "satisfied" ? "Mark satisfied" : "Mark not satisfied"}
        busy={busy}
        onConfirm={() => void resolve()}
        onCancel={() => setConfirming(false)}
      >
        <p>This is recorded permanently in the audit trail under your account and cannot be changed.</p>
        {outcome === "satisfied" && (
          <p className="mt-2">It does not approve the request: {nameOf(condition.approver_id)} must still re-approve.</p>
        )}
      </ConfirmDialog>
    </li>
  );
}

function VoiceCard({
  request,
  calls,
  nameOf,
  onScheduled,
}: {
  request: RequestDetail;
  calls: PollState<VoiceCall[]>;
  nameOf: (id: string) => string;
  onScheduled: () => void;
}) {
  const session = useSession();
  const { can } = useAuth();
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const [confirmRedial, setConfirmRedial] = useState(false);
  const provider = session.environment.voice_provider;
  const voiceOn = provider !== "disabled";
  const canSchedule = voiceOn && can("voice:calls:create") && request.status === "pending";

  async function schedule(redial: boolean) {
    if (busy) return;
    setBusy(true);
    try {
      const result = await api.scheduleVoiceCalls(request.id, redial);
      toast(
        result.created > 0 ? "success" : "info",
        result.created > 0
          ? `${result.created} call(s) queued. The worker places them; the request is not authorized by a call.`
          : "No new calls: they were already scheduled (or no one needs calling).",
      );
      if (result.skipped.length) {
        toast("info", `Skipped: ${result.skipped.map((s) => `${nameOf(s.approver_id)} (${s.reason.replace(/_/g, " ")})`).join(", ")}`);
      }
      onScheduled();
    } catch (caught) {
      toast("error", caught instanceof ApiError ? caught.userMessage : "Scheduling failed.");
    } finally {
      setBusy(false);
      setConfirmRedial(false);
    }
  }

  return (
    <Card
      title={
        <span className="flex flex-wrap items-center gap-2">
          Voice calls <VoiceModeBadge provider={provider} />
        </span>
      }
      actions={
        canSchedule ? (
          <>
            <Button variant="primary" disabled={busy} onClick={() => void schedule(false)}>
              {busy ? "Scheduling…" : "Schedule calls"}
            </Button>
            <Button disabled={busy} onClick={() => setConfirmRedial(true)}>
              Redial unanswered
            </Button>
          </>
        ) : null
      }
    >
      {!can("voice:calls:read") ? (
        <Unavailable scope="voice:calls:read" what="Call status" />
      ) : calls.error && !calls.data ? (
        <ErrorState error={calls.error} onRetry={calls.refresh} compact />
      ) : !calls.data ? (
        <LoadingBlock />
      ) : calls.data.length === 0 ? (
        <EmptyState title="No calls for this request.">
          {!voiceOn
            ? "Voice calls are disabled on this server."
            : canSchedule
              ? "Use “Schedule calls” to phone each approver through the durable job queue."
              : request.status === "pending" && !can("voice:calls:create")
                ? "Your account lacks the voice:calls:create scope."
                : null}
        </EmptyState>
      ) : (
        <div className="overflow-x-auto">
          <table className="min-w-full text-left text-sm">
            <caption className="sr-only">Voice calls</caption>
            <thead className="border-b border-slate-200 text-xs uppercase tracking-wide text-slate-500">
              <tr>
                <th scope="col" className="py-2 pr-4">Approver</th>
                <th scope="col" className="py-2 pr-4">Call</th>
                <th scope="col" className="py-2 pr-4">Decision</th>
                <th scope="col" className="py-2">Updated</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {calls.data.map((call) => (
                <tr key={call.id} className="align-top">
                  <td className="py-2 pr-4">
                    <span className="font-medium text-slate-900">{nameOf(call.approver_id)}</span>
                    {call.generation > 1 && <span className="ml-1 text-xs text-slate-500">attempt {call.generation}</span>}
                    {call.provider === "mock" && <p className="text-xs font-semibold text-amber-800">simulated</p>}
                  </td>
                  <td className="py-2 pr-4">
                    {callStatusLabel(call.status)}
                    {call.outcome && <p className="text-xs text-slate-500">{call.outcome.replace(/_/g, " ")}</p>}
                  </td>
                  <td className="py-2 pr-4">
                    {decisionStatusLabel(call.decision_status)}
                    {call.decision_candidate && (
                      <p className="text-xs text-slate-500">
                        heard: {call.decision_candidate.replace(/_/g, " ")}
                        {call.decision_confidence !== null && ` (${Math.round(call.decision_confidence * 100)}%)`}
                      </p>
                    )}
                  </td>
                  <td className="py-2 text-xs text-slate-500">{formatRelative(call.updated_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="mt-2 text-xs text-slate-500">
            A completed call is not a vote. Only an explicit, confident, identity-confirmed decision on a verified call is
            recorded, and the policy still decides the request.
          </p>
        </div>
      )}
      <ConfirmDialog
        open={confirmRedial}
        title="Redial unanswered approvers?"
        confirmLabel="Place new calls"
        busy={busy}
        onConfirm={() => void schedule(true)}
        onCancel={() => setConfirmRedial(false)}
      >
        A new call is placed to each approver whose latest call ended without a decision (bounded by the server’s attempt
        limit). {provider === "bolna" ? "These are real phone calls." : "Calls are simulated in this environment."}
      </ConfirmDialog>
    </Card>
  );
}
