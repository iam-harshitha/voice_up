import { useEffect, useState, type FormEvent } from "react";
import { ApiError } from "../api/client";
import { api } from "../api/endpoints";
import type { Approver, Policy, PolicyInput } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { Timeline } from "../components/Timeline";
import {
  Button,
  Card,
  ConfirmDialog,
  EmptyState,
  ErrorState,
  Field,
  LoadingBlock,
  Unavailable,
  cx,
  inputClass,
  useToast,
} from "../components/ui";
import { usePolling } from "../hooks/usePolling";
import { formatDateTime, formatDuration } from "../lib/format";

export interface PolicyFormValues {
  name: string;
  description: string;
  approval_mode: "unanimous" | "quorum";
  required_approvals: 1 | 2;
  approver_ids: string[];
  veto_mode: "any_rejection" | "designated" | "none";
  veto_approver_ids: string[];
  backup_approver_ids: string[];
  deadline_minutes: string;
  escalation_minutes: string;
  max_escalations: string;
  auto_dial: boolean;
}

export const EMPTY_POLICY: PolicyFormValues = {
  name: "",
  description: "",
  approval_mode: "unanimous",
  required_approvals: 1,
  approver_ids: [],
  veto_mode: "any_rejection",
  veto_approver_ids: [],
  backup_approver_ids: [],
  deadline_minutes: "",
  escalation_minutes: "",
  max_escalations: "",
  auto_dial: false,
};

function fromPolicy(p: Policy): PolicyFormValues {
  return {
    name: p.name,
    description: p.description ?? "",
    approval_mode: p.approval_mode === "quorum" ? "quorum" : "unanimous",
    required_approvals: p.required_approvals === 2 ? 2 : 1,
    approver_ids: p.approvers.map((a) => a.id),
    veto_mode: (["any_rejection", "designated", "none"].includes(p.veto_mode) ? p.veto_mode : "any_rejection") as PolicyFormValues["veto_mode"],
    veto_approver_ids: [...p.veto_approver_ids],
    backup_approver_ids: p.backup_approvers.map((a) => a.id),
    deadline_minutes: p.decision_deadline_seconds ? String(p.decision_deadline_seconds / 60) : "",
    escalation_minutes: p.escalation_after_seconds ? String(p.escalation_after_seconds / 60) : "",
    max_escalations: p.escalation_after_seconds ? String(p.max_escalations) : "",
    auto_dial: p.auto_dial,
  };
}

function minutes(value: string): number | null | "invalid" {
  if (!value.trim()) return null;
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0 || Math.round(n * 60) !== n * 60) return "invalid";
  return Math.round(n * 60);
}

/**
 * Catches obvious mistakes before submission. The backend validates the whole policy
 * again and is authoritative; its error message is shown when it disagrees.
 */
export function validatePolicyForm(v: PolicyFormValues): { errors: Record<string, string>; payload: PolicyInput | null } {
  const errors: Record<string, string> = {};
  const name = v.name.trim();
  if (!name) errors.name = "Name is required.";
  else if (name.length > 200) errors.name = "At most 200 characters.";
  const count = v.approver_ids.length;
  if (count === 0) errors.approver_ids = "Choose at least one approver.";
  else if (count > 5) errors.approver_ids = "At most 5 primary approvers.";
  if (v.approval_mode === "unanimous") {
    if (count && count !== v.required_approvals)
      errors.required_approvals = `Unanimous policies need required approvals = number of approvers (${count}).`;
    if (count > 2) errors.approver_ids = "Unanimous policies support 1 or 2 approvers (required approvals is 1 or 2).";
    if (v.veto_mode !== "any_rejection") errors.veto_mode = "Unanimous policies use “any rejection vetoes”.";
  } else if (count && v.required_approvals > count) {
    errors.required_approvals = "Required approvals cannot exceed the number of approvers.";
  }
  if (v.veto_mode === "designated" && v.veto_approver_ids.length === 0)
    errors.veto_approver_ids = "Choose at least one veto holder.";
  if (v.veto_approver_ids.some((id) => !v.approver_ids.includes(id)))
    errors.veto_approver_ids = "Veto holders must be primary approvers.";
  if (v.backup_approver_ids.length > 3) errors.backup_approver_ids = "At most 3 backups.";
  if (v.backup_approver_ids.some((id) => v.approver_ids.includes(id)))
    errors.backup_approver_ids = "An approver cannot be both primary and backup.";
  const deadline = minutes(v.deadline_minutes);
  const escalation = minutes(v.escalation_minutes);
  if (deadline === "invalid" || (typeof deadline === "number" && (deadline < 60 || deadline > 30 * 86400)))
    errors.deadline_minutes = "Between 1 minute and 30 days (43200 minutes).";
  if (escalation === "invalid" || (typeof escalation === "number" && (escalation < 60 || escalation > 7 * 86400)))
    errors.escalation_minutes = "Between 1 minute and 7 days (10080 minutes).";
  let maxEscalations: number | null = null;
  if (typeof escalation === "number") {
    if (v.backup_approver_ids.length === 0) errors.escalation_minutes = "Escalation needs at least one backup approver.";
    if (typeof deadline === "number" && escalation >= deadline)
      errors.escalation_minutes = "Escalation must start before the deadline.";
    if (v.max_escalations.trim()) {
      const n = Number(v.max_escalations);
      if (!Number.isInteger(n) || n < 1 || n > Math.max(1, v.backup_approver_ids.length))
        errors.max_escalations = "Between 1 and the number of backups.";
      else maxEscalations = n;
    }
  } else if (v.max_escalations.trim()) {
    errors.max_escalations = "Only used with escalation.";
  }
  if (Object.keys(errors).length) return { errors, payload: null };
  return {
    errors,
    payload: {
      name,
      description: v.description.trim() || null,
      approval_mode: v.approval_mode,
      required_approvals: v.required_approvals,
      approver_ids: v.approver_ids,
      veto_mode: v.veto_mode,
      veto_approver_ids: v.veto_mode === "designated" ? v.veto_approver_ids : [],
      backup_approver_ids: v.backup_approver_ids,
      decision_deadline_seconds: deadline as number | null,
      escalation_after_seconds: escalation as number | null,
      max_escalations: maxEscalations,
      auto_dial: v.auto_dial,
    },
  };
}

function ApproverPicker({
  label,
  approvers,
  selected,
  onChange,
  error,
  hint,
  disabledIds = [],
}: {
  label: string;
  approvers: Approver[];
  selected: string[];
  onChange: (ids: string[]) => void;
  error?: string;
  hint?: string;
  disabledIds?: string[];
}) {
  return (
    <fieldset className="space-y-1">
      <legend className="text-sm font-medium text-slate-800">{label}</legend>
      {hint && <p className="text-xs text-slate-500">{hint}</p>}
      <div className="flex flex-wrap gap-2">
        {approvers.map((a) => {
          const checked = selected.includes(a.id);
          const disabled = (!a.is_active && !checked) || disabledIds.includes(a.id);
          return (
            <label
              key={a.id}
              className={cx(
                "flex items-center gap-1.5 rounded-md px-2 py-1 text-sm ring-1",
                checked ? "bg-slate-900 text-white ring-slate-900" : "bg-white text-slate-800 ring-slate-300",
                disabled && "opacity-50",
              )}
            >
              <input
                type="checkbox"
                className="sr-only"
                checked={checked}
                disabled={disabled}
                onChange={() => onChange(checked ? selected.filter((id) => id !== a.id) : [...selected, a.id])}
              />
              {checked ? "✓ " : ""}
              {a.name}
              {!a.is_active && " (inactive)"}
              {checked && selected.length > 1 && <span className="text-xs opacity-75">#{selected.indexOf(a.id) + 1}</span>}
            </label>
          );
        })}
      </div>
      {error && <p className="text-xs font-medium text-red-700">{error}</p>}
    </fieldset>
  );
}

export function PolicyForm({
  initial,
  approvers,
  submitLabel,
  onSubmit,
  onCancel,
}: {
  initial: PolicyFormValues;
  approvers: Approver[];
  submitLabel: string;
  onSubmit: (payload: PolicyInput) => Promise<void>;
  onCancel?: () => void;
}) {
  const [v, setV] = useState<PolicyFormValues>(initial);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [serverError, setServerError] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);
  const set = <K extends keyof PolicyFormValues>(key: K, value: PolicyFormValues[K]) => setV((cur) => ({ ...cur, [key]: value }));

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    const { errors: found, payload } = validatePolicyForm(v);
    setErrors(found);
    if (!payload) return;
    setBusy(true);
    setServerError(null);
    try {
      await onSubmit(payload);
    } catch (caught) {
      setServerError(caught instanceof ApiError ? caught : new ApiError(0, "client_error", String(caught)));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="space-y-4" noValidate>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Name" error={errors.name}>
          {(props) => <input {...props} className={inputClass} maxLength={200} value={v.name} onChange={(e) => set("name", e.target.value)} />}
        </Field>
        <Field label="Description (optional)">
          {(props) => <input {...props} className={inputClass} maxLength={2000} value={v.description} onChange={(e) => set("description", e.target.value)} />}
        </Field>
        <Field label="Approval mode" error={errors.approval_mode}>
          {(props) => (
            <select
              {...props}
              className={inputClass}
              value={v.approval_mode}
              onChange={(e) => {
                const mode = e.target.value as PolicyFormValues["approval_mode"];
                setV((cur) => ({ ...cur, approval_mode: mode, veto_mode: mode === "unanimous" ? "any_rejection" : cur.veto_mode }));
              }}
            >
              <option value="unanimous">Unanimous — every primary approver</option>
              <option value="quorum">Quorum — a number of the primary approvers</option>
            </select>
          )}
        </Field>
        <Field label="Required approvals" error={errors.required_approvals} hint="The backend supports 1 or 2.">
          {(props) => (
            <select {...props} className={inputClass} value={v.required_approvals} onChange={(e) => set("required_approvals", Number(e.target.value) === 2 ? 2 : 1)}>
              <option value={1}>1</option>
              <option value={2}>2</option>
            </select>
          )}
        </Field>
      </div>
      <ApproverPicker
        label="Primary approvers (each holds a seat)"
        approvers={approvers}
        selected={v.approver_ids}
        onChange={(ids) => set("approver_ids", ids)}
        error={errors.approver_ids}
        disabledIds={v.backup_approver_ids}
      />
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Veto rule" error={errors.veto_mode}>
          {(props) => (
            <select
              {...props}
              className={inputClass}
              value={v.veto_mode}
              disabled={v.approval_mode === "unanimous"}
              onChange={(e) => set("veto_mode", e.target.value as PolicyFormValues["veto_mode"])}
            >
              <option value="any_rejection">Any rejection vetoes</option>
              <option value="designated">Only designated veto holders veto</option>
              <option value="none">No vetoes (rejections count against the quorum)</option>
            </select>
          )}
        </Field>
      </div>
      {v.veto_mode === "designated" && (
        <ApproverPicker
          label="Veto holders"
          approvers={approvers.filter((a) => v.approver_ids.includes(a.id))}
          selected={v.veto_approver_ids}
          onChange={(ids) => set("veto_approver_ids", ids)}
          error={errors.veto_approver_ids}
        />
      )}
      <ApproverPicker
        label="Backup approvers (escalation order)"
        hint="A backup replaces an undecided seat after the escalation interval. Up to 3."
        approvers={approvers}
        selected={v.backup_approver_ids}
        onChange={(ids) => set("backup_approver_ids", ids)}
        error={errors.backup_approver_ids}
        disabledIds={v.approver_ids}
      />
      <div className="grid gap-4 sm:grid-cols-3">
        <Field label="Decision deadline (minutes)" error={errors.deadline_minutes} hint="Empty = no deadline.">
          {(props) => <input {...props} inputMode="numeric" className={inputClass} value={v.deadline_minutes} onChange={(e) => set("deadline_minutes", e.target.value)} />}
        </Field>
        <Field label="Escalate after (minutes)" error={errors.escalation_minutes} hint="Empty = no escalation.">
          {(props) => <input {...props} inputMode="numeric" className={inputClass} value={v.escalation_minutes} onChange={(e) => set("escalation_minutes", e.target.value)} />}
        </Field>
        <Field label="Max escalation steps" error={errors.max_escalations} hint="Empty = number of backups.">
          {(props) => <input {...props} inputMode="numeric" className={inputClass} value={v.max_escalations} onChange={(e) => set("max_escalations", e.target.value)} />}
        </Field>
      </div>
      <label className="flex items-start gap-2 text-sm text-slate-800">
        <input
          type="checkbox"
          className="mt-0.5"
          checked={v.auto_dial}
          onChange={(e) => set("auto_dial", e.target.checked)}
        />
        <span>
          Auto-dial approvers
          <span className="block text-xs text-slate-500">
            Call the approvers as soon as a request is created under this policy, instead of waiting for “Schedule calls”.
          </span>
        </span>
      </label>
      <p className="text-xs text-slate-500">
        Conditions always require the approver’s fresh, unconditional re-approval; that rule is not configurable.
      </p>
      {serverError && <ErrorState error={serverError} compact />}
      <div className="flex gap-2">
        <Button type="submit" variant="primary" disabled={busy}>
          {busy ? "Saving…" : submitLabel}
        </Button>
        {onCancel && (
          <Button onClick={onCancel} disabled={busy}>
            Cancel
          </Button>
        )}
      </div>
    </form>
  );
}

function PolicyDetail({ policy, onChanged }: { policy: Policy; onChanged: () => void }) {
  const { can } = useAuth();
  const toast = useToast();
  const [editing, setEditing] = useState(false);
  const [confirmToggle, setConfirmToggle] = useState(false);
  const [busy, setBusy] = useState(false);
  const approvers = usePolling("approvers:policy-form", (signal) => api.listApprovers({ signal }), {
    intervalMs: null,
    enabled: editing && can("approvers:read"),
  });
  const history = usePolling(`policy-audit:${policy.id}:${policy.version}`, (signal) => api.policyTimeline(policy.id, { signal }), {
    intervalMs: null,
    enabled: can("audit:read"),
  });
  useEffect(() => setEditing(false), [policy.id]);

  async function toggleActive() {
    setBusy(true);
    try {
      await api.updatePolicy(policy.id, { is_active: !policy.is_active });
      toast("success", policy.is_active ? "Policy deactivated." : "Policy activated.");
      onChanged();
    } catch (caught) {
      toast("error", caught instanceof ApiError ? caught.userMessage : "Update failed.");
    } finally {
      setBusy(false);
      setConfirmToggle(false);
    }
  }

  const rows: [string, string][] = [
    ["Mode", policy.approval_mode === "quorum" ? `Quorum: ${policy.required_approvals} of ${policy.approvers.length}` : `Unanimous (${policy.required_approvals})`],
    ["Approvers", policy.approvers.map((a) => a.name + (a.is_active ? "" : " (inactive)")).join(", ")],
    ["Veto", policy.veto_mode.replace(/_/g, " ") + (policy.veto_approver_ids.length ? ` — ${policy.approvers.filter((a) => policy.veto_approver_ids.includes(a.id)).map((a) => a.name).join(", ")}` : "")],
    ["Backups", policy.backup_approvers.map((a) => a.name).join(", ") || "None"],
    ["Deadline", formatDuration(policy.decision_deadline_seconds)],
    ["Escalation", policy.escalation_after_seconds ? `Every ${formatDuration(policy.escalation_after_seconds)}, up to ${policy.max_escalations} step(s)` : "None"],
    ["Auto-dial", policy.auto_dial ? "On: approvers are called when a request is created" : "Off: calls are scheduled manually"],
    ["Updated", formatDateTime(policy.updated_at)],
  ];

  return (
    <Card
      title={`${policy.name} · v${policy.version}${policy.is_active ? "" : " (inactive)"}`}
      actions={
        can("policies:write") && !editing ? (
          <>
            <Button onClick={() => setEditing(true)}>Edit</Button>
            <Button variant={policy.is_active ? "danger" : "secondary"} onClick={() => setConfirmToggle(true)}>
              {policy.is_active ? "Deactivate" : "Activate"}
            </Button>
          </>
        ) : null
      }
    >
      {editing ? (
        approvers.error ? (
          <ErrorState error={approvers.error} onRetry={approvers.refresh} />
        ) : !approvers.data ? (
          <LoadingBlock label="Loading approvers" />
        ) : (
          <PolicyForm
            initial={fromPolicy(policy)}
            approvers={approvers.data.items}
            submitLabel="Save changes"
            onCancel={() => setEditing(false)}
            onSubmit={async (payload) => {
              // PATCH treats an omitted max_escalations as "default to the number of
              // backups"; an explicit null is rejected by the API.
              const { max_escalations, ...rest } = payload;
              const updated = await api.updatePolicy(
                policy.id,
                max_escalations === null || max_escalations === undefined ? rest : payload,
              );
              toast("success", updated.version !== policy.version ? `Saved as version ${updated.version}. Existing requests keep their snapshot.` : "No changes.");
              setEditing(false);
              onChanged();
            }}
          />
        )
      ) : (
        <>
          {policy.description && <p className="mb-3 text-sm text-slate-700">{policy.description}</p>}
          <dl className="space-y-1.5 text-sm">
            {rows.map(([label, value]) => (
              <div key={label} className="grid grid-cols-3 gap-2">
                <dt className="text-slate-500">{label}</dt>
                <dd className="col-span-2 text-slate-900">{value}</dd>
              </div>
            ))}
          </dl>
          <p className="mt-2 font-mono text-xs text-slate-400">{policy.id}</p>
          {can("audit:read") && (
            <details className="mt-4">
              <summary className="cursor-pointer text-sm font-medium text-sky-700">Change history</summary>
              <div className="mt-3">
                {history.error ? <ErrorState error={history.error} compact /> : !history.data ? <LoadingBlock /> : (
                  <Timeline events={history.data.items} nameOf={(id) => id.slice(0, 8)} />
                )}
              </div>
            </details>
          )}
        </>
      )}
      <ConfirmDialog
        open={confirmToggle}
        title={policy.is_active ? "Deactivate this policy?" : "Activate this policy?"}
        confirmLabel={policy.is_active ? "Deactivate" : "Activate"}
        danger={policy.is_active}
        busy={busy}
        onConfirm={() => void toggleActive()}
        onCancel={() => setConfirmToggle(false)}
      >
        {policy.is_active
          ? "New requests can no longer use it. Existing requests are unaffected and keep their snapshot."
          : "New requests may use it again."}
      </ConfirmDialog>
    </Card>
  );
}

export function PoliciesPage() {
  const { can } = useAuth();
  const toast = useToast();
  const [selected, setSelected] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const policies = usePolling("policies", (signal) => api.listPolicies({ signal }), {
    intervalMs: null,
    enabled: can("policies:read"),
  });
  const approvers = usePolling("approvers:create-policy", (signal) => api.listApprovers({ signal }), {
    intervalMs: null,
    enabled: creating && can("approvers:read"),
  });

  if (!can("policies:read")) return <Unavailable scope="policies:read" what="Policies" />;
  const items = policies.data?.items ?? [];
  const current = items.find((p) => p.id === selected) ?? items[0] ?? null;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <h1 className="text-xl font-semibold text-slate-900">Policies</h1>
          <p className="text-sm text-slate-500">Who must approve, and how. Edits create a new version; existing requests never change.</p>
        </div>
        {can("policies:write") && !creating && (
          <Button variant="primary" onClick={() => setCreating(true)} disabled={!can("approvers:read")} title={can("approvers:read") ? undefined : "Requires approvers:read"}>
            New policy
          </Button>
        )}
      </div>

      {creating && (
        <Card title="New policy">
          {approvers.error ? (
            <ErrorState error={approvers.error} onRetry={approvers.refresh} />
          ) : !approvers.data ? (
            <LoadingBlock label="Loading approvers" />
          ) : approvers.data.items.length === 0 ? (
            <EmptyState title="Add approvers first." />
          ) : (
            <PolicyForm
              initial={EMPTY_POLICY}
              approvers={approvers.data.items}
              submitLabel="Create policy"
              onCancel={() => setCreating(false)}
              onSubmit={async (payload) => {
                const created = await api.createPolicy(payload);
                toast("success", `Policy “${created.name}” created.`);
                setCreating(false);
                setSelected(created.id);
                policies.refresh();
              }}
            />
          )}
        </Card>
      )}

      {policies.error && !policies.data && <ErrorState error={policies.error} onRetry={policies.refresh} />}
      {!policies.data && policies.loading && <LoadingBlock label="Loading policies" />}
      {policies.data && items.length === 0 && !creating && <EmptyState title="No policies yet." />}
      {items.length > 0 && (
        <div className="grid gap-4 lg:grid-cols-3">
          <Card title={`All policies (${policies.data?.total ?? 0})`}>
            <ul className="space-y-1">
              {items.map((p) => (
                <li key={p.id}>
                  <button
                    type="button"
                    onClick={() => setSelected(p.id)}
                    aria-current={current?.id === p.id}
                    className={cx(
                      "w-full rounded-md px-2 py-1.5 text-left text-sm",
                      current?.id === p.id ? "bg-slate-900 text-white" : "hover:bg-slate-100",
                    )}
                  >
                    <span className="font-medium">{p.name}</span>
                    <span className="block text-xs opacity-75">
                      {p.approval_mode === "quorum" ? `${p.required_approvals} of ${p.approvers.length}` : "unanimous"} · v{p.version}
                      {p.is_active ? "" : " · inactive"}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          </Card>
          <div className="lg:col-span-2">{current && <PolicyDetail policy={current} onChanged={policies.refresh} />}</div>
        </div>
      )}
    </div>
  );
}
