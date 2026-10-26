import { useRef, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ApiError } from "../api/client";
import { api } from "../api/endpoints";
import { useAuth } from "../auth/AuthContext";
import { Button, Card, ErrorState, Field, LoadingBlock, Unavailable, inputClass, useToast } from "../components/ui";
import { usePolling } from "../hooks/usePolling";

const MAX_CONTEXT_BYTES = 16 * 1024;

export const DEPLOY_EXAMPLE = {
  action: "deploy_to_production",
  summary:
    "Deploy release v2.4 to production. Automated tests passed, but the release includes a database " +
    "migration. Verify the backup and migration safety before proceeding.",
  context: {
    release: "v2.4",
    tests: "passed",
    includes_database_migration: true,
    backup_verified: false,
    target: "simulated-production",
  },
};

export function validateRequestForm(values: { policyId: string; action: string; summary: string; context: string }) {
  const errors: Record<string, string> = {};
  if (!values.policyId) errors.policyId = "Choose a policy.";
  const action = values.action.trim();
  if (!action) errors.action = "Action is required.";
  else if (action.length > 200) errors.action = "At most 200 characters.";
  const summary = values.summary.trim();
  if (!summary) errors.summary = "Summary is required.";
  else if (summary.length > 2000) errors.summary = "At most 2000 characters.";
  let context: Record<string, unknown> = {};
  if (values.context.trim()) {
    try {
      const parsed: unknown = JSON.parse(values.context);
      if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
        errors.context = "Context must be a JSON object, e.g. {\"release\": \"v2.4\"}.";
      } else if (new TextEncoder().encode(JSON.stringify(parsed)).length > MAX_CONTEXT_BYTES) {
        errors.context = "Context must be at most 16 KB.";
      } else {
        context = parsed as Record<string, unknown>;
      }
    } catch {
      errors.context = "Context is not valid JSON.";
    }
  }
  return { errors, payload: { policy_id: values.policyId, action, summary, context } };
}

export function NewRequestPage() {
  const { can } = useAuth();
  const navigate = useNavigate();
  const toast = useToast();
  const policies = usePolling("policies:new-request", (signal) => api.listPolicies({ signal }), {
    intervalMs: null,
    enabled: can("policies:read"),
  });
  const [policyId, setPolicyId] = useState("");
  const [action, setAction] = useState("");
  const [summary, setSummary] = useState("");
  const [context, setContext] = useState("");
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [submitError, setSubmitError] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);
  // Guards against double submission within this page. It is not distributed
  // idempotency: the API has no idempotency key for request creation.
  const submitted = useRef(false);

  if (!can("requests:create")) return <Unavailable scope="requests:create" what="Creating requests" />;

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy || submitted.current) return;
    const { errors: found, payload } = validateRequestForm({ policyId, action, summary, context });
    setErrors(found);
    if (Object.keys(found).length) return;
    submitted.current = true;
    setBusy(true);
    setSubmitError(null);
    try {
      const created = await api.createRequest(payload);
      toast("success", `Request ${created.id} created — status: ${created.status}.`);
      navigate(`/requests/${created.id}`);
    } catch (caught) {
      // A network failure or timeout means creation may or may not have happened.
      const error = caught instanceof ApiError ? caught : new ApiError(0, "client_error", String(caught));
      setSubmitError(error);
      if (error.status === 0 || error.status >= 500) {
        setErrors({ form: "The outcome is unknown. Check the request list before submitting again." });
      } else {
        submitted.current = false;
      }
    } finally {
      setBusy(false);
    }
  }

  const active = policies.data?.items.filter((p) => p.is_active) ?? [];

  return (
    <div className="max-w-3xl space-y-4">
      <div>
        <h1 className="text-xl font-semibold text-slate-900">New approval request</h1>
        <p className="text-sm text-slate-500">
          Normally an agent creates requests through the API or MCP. Creating one never authorizes anything.
        </p>
      </div>
      <Card
        actions={
          <Button
            onClick={() => {
              setAction(DEPLOY_EXAMPLE.action);
              setSummary(DEPLOY_EXAMPLE.summary);
              setContext(JSON.stringify(DEPLOY_EXAMPLE.context, null, 2));
            }}
          >
            Use the production-deployment example
          </Button>
        }
        title="Request"
      >
        <form onSubmit={submit} className="space-y-4" noValidate>
          <Field label="Policy" error={errors.policyId} hint="Only active policies can accept new requests.">
            {(props) =>
              !can("policies:read") ? (
                <input
                  {...props}
                  className={inputClass}
                  placeholder="Policy ID (UUID)"
                  value={policyId}
                  onChange={(e) => setPolicyId(e.target.value.trim())}
                />
              ) : policies.error ? (
                <ErrorState error={policies.error} onRetry={policies.refresh} compact />
              ) : !policies.data ? (
                <LoadingBlock label="Loading policies" />
              ) : active.length === 0 ? (
                <p className="text-sm text-slate-600">
                  No active policies. <Link to="/policies" className="text-sky-700 underline">Create one first.</Link>
                </p>
              ) : (
                <select {...props} className={inputClass} value={policyId} onChange={(e) => setPolicyId(e.target.value)}>
                  <option value="">Select a policy…</option>
                  {active.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name} — {p.approval_mode === "quorum" ? `${p.required_approvals} of ${p.approvers.length}` : "unanimous"} (v{p.version})
                    </option>
                  ))}
                </select>
              )
            }
          </Field>
          <Field label="Action" error={errors.action} hint="A short identifier, e.g. deploy_to_production.">
            {(props) => (
              <input {...props} className={inputClass} maxLength={200} value={action} onChange={(e) => setAction(e.target.value)} />
            )}
          </Field>
          <Field label="Summary" error={errors.summary} hint="What will happen and why, in words an approver will hear on the phone.">
            {(props) => (
              <textarea {...props} rows={4} maxLength={2000} className={inputClass} value={summary} onChange={(e) => setSummary(e.target.value)} />
            )}
          </Field>
          <Field label="Context (JSON object, optional)" error={errors.context} hint="Structured facts for approvers. At most 16 KB.">
            {(props) => (
              <textarea
                {...props}
                rows={7}
                className={`${inputClass} font-mono`}
                value={context}
                onChange={(e) => setContext(e.target.value)}
                spellCheck={false}
              />
            )}
          </Field>
          {errors.form && (
            <p role="alert" className="text-sm font-medium text-amber-900">
              {errors.form}
            </p>
          )}
          {submitError && <ErrorState error={submitError} compact />}
          <Button type="submit" variant="primary" disabled={busy}>
            {busy ? "Creating…" : "Create request"}
          </Button>
        </form>
      </Card>
    </div>
  );
}
