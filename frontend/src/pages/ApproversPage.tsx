import { useState, type FormEvent } from "react";
import { ApiError } from "../api/client";
import { api } from "../api/endpoints";
import type { Approver } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import {
  Button,
  Card,
  ConfirmDialog,
  EmptyState,
  ErrorState,
  Field,
  LoadingBlock,
  Unavailable,
  inputClass,
  useToast,
} from "../components/ui";
import { usePolling } from "../hooks/usePolling";
import { maskEmail, maskPhone } from "../lib/format";

const E164 = /^\+[1-9]\d{1,14}$/;
const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export function validateApproverForm(v: { name: string; phone: string; email: string }) {
  const errors: Record<string, string> = {};
  const name = v.name.trim();
  const phone = v.phone.trim();
  const email = v.email.trim();
  if (!name) errors.name = "Name is required.";
  else if (name.length > 200) errors.name = "At most 200 characters.";
  if (phone && !E164.test(phone)) errors.phone = "Use E.164 format, e.g. +14155550101.";
  if (email && !EMAIL.test(email)) errors.email = "Enter a valid email address.";
  if (!phone && !email) errors.phone = "Provide a phone number or an email address.";
  return { errors, payload: { name, phone_number: phone || null, email: email || null } };
}

function ApproverForm({
  initial,
  submitLabel,
  onSubmit,
  onCancel,
}: {
  initial?: Approver;
  submitLabel: string;
  onSubmit: (payload: { name: string; phone_number: string | null; email: string | null }) => Promise<void>;
  onCancel: () => void;
}) {
  const [name, setName] = useState(initial?.name ?? "");
  const [phone, setPhone] = useState(initial?.phone_number ?? "");
  const [email, setEmail] = useState(initial?.email ?? "");
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [serverError, setServerError] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    const { errors: found, payload } = validateApproverForm({ name, phone, email });
    setErrors(found);
    if (Object.keys(found).length) return;
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
    <form onSubmit={submit} className="grid gap-3 sm:grid-cols-3" noValidate>
      <Field label="Name" error={errors.name}>
        {(props) => <input {...props} className={inputClass} maxLength={200} value={name} onChange={(e) => setName(e.target.value)} />}
      </Field>
      <Field label="Phone (E.164)" error={errors.phone} hint="Used for approval calls.">
        {(props) => <input {...props} className={inputClass} autoComplete="off" value={phone} onChange={(e) => setPhone(e.target.value)} />}
      </Field>
      <Field label="Email" error={errors.email}>
        {(props) => <input {...props} type="email" autoComplete="off" className={inputClass} value={email} onChange={(e) => setEmail(e.target.value)} />}
      </Field>
      {serverError && (
        <div className="sm:col-span-3">
          <ErrorState error={serverError} compact />
        </div>
      )}
      <div className="flex gap-2 sm:col-span-3">
        <Button type="submit" variant="primary" disabled={busy}>
          {busy ? "Saving…" : submitLabel}
        </Button>
        <Button onClick={onCancel} disabled={busy}>
          Cancel
        </Button>
      </div>
    </form>
  );
}

export function ApproversPage() {
  const { can } = useAuth();
  const toast = useToast();
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const [revealed, setRevealed] = useState<Set<string>>(new Set());
  const [confirm, setConfirm] = useState<Approver | null>(null);
  const [busy, setBusy] = useState(false);
  const list = usePolling("approvers", (signal) => api.listApprovers({ signal }), {
    intervalMs: null,
    enabled: can("approvers:read"),
  });

  if (!can("approvers:read")) return <Unavailable scope="approvers:read" what="Approvers" />;
  const canWrite = can("approvers:write");

  async function toggleActive(approver: Approver) {
    setBusy(true);
    try {
      await api.updateApprover(approver.id, { is_active: !approver.is_active });
      toast("success", approver.is_active ? `${approver.name} deactivated.` : `${approver.name} reactivated.`);
      list.refresh();
    } catch (caught) {
      toast("error", caught instanceof ApiError ? caught.userMessage : "Update failed.");
    } finally {
      setBusy(false);
      setConfirm(null);
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <h1 className="text-xl font-semibold text-slate-900">Approvers</h1>
          <p className="text-sm text-slate-500">
            People who can be asked to approve. Contact details are personal data and are masked by default.
          </p>
        </div>
        {canWrite && !creating && (
          <Button variant="primary" onClick={() => setCreating(true)}>
            Add approver
          </Button>
        )}
      </div>

      {creating && (
        <Card title="New approver">
          <ApproverForm
            submitLabel="Add approver"
            onCancel={() => setCreating(false)}
            onSubmit={async (payload) => {
              const created = await api.createApprover(payload);
              toast("success", `${created.name} added.`);
              setCreating(false);
              list.refresh();
            }}
          />
        </Card>
      )}

      <Card>
        {list.error && !list.data && <ErrorState error={list.error} onRetry={list.refresh} />}
        {!list.data && list.loading && <LoadingBlock label="Loading approvers" />}
        {list.data && list.data.items.length === 0 && <EmptyState title="No approvers yet." />}
        {list.data && list.data.items.length > 0 && (
          <div className="overflow-x-auto">
            <table className="min-w-full text-left text-sm">
              <caption className="sr-only">Approvers</caption>
              <thead className="border-b border-slate-200 text-xs uppercase tracking-wide text-slate-500">
                <tr>
                  <th scope="col" className="py-2 pr-4">Name</th>
                  <th scope="col" className="py-2 pr-4">Phone</th>
                  <th scope="col" className="py-2 pr-4">Email</th>
                  <th scope="col" className="py-2 pr-4">Status</th>
                  <th scope="col" className="py-2">
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {list.data.items.map((a) =>
                  editing === a.id ? (
                    <tr key={a.id}>
                      <td colSpan={5} className="py-3">
                        <ApproverForm
                          initial={a}
                          submitLabel="Save"
                          onCancel={() => setEditing(null)}
                          onSubmit={async (payload) => {
                            await api.updateApprover(a.id, payload);
                            toast("success", "Approver updated. Existing requests keep the approvers they were created with.");
                            setEditing(null);
                            list.refresh();
                          }}
                        />
                      </td>
                    </tr>
                  ) : (
                    <tr key={a.id}>
                      <td className="py-2 pr-4 font-medium text-slate-900">{a.name}</td>
                      <td className="py-2 pr-4 font-mono text-xs">{revealed.has(a.id) ? a.phone_number ?? "—" : maskPhone(a.phone_number)}</td>
                      <td className="py-2 pr-4 text-xs">{revealed.has(a.id) ? a.email ?? "—" : maskEmail(a.email)}</td>
                      <td className="py-2 pr-4">
                        <span className={a.is_active ? "text-emerald-800" : "text-slate-500"}>{a.is_active ? "● Active" : "○ Inactive"}</span>
                      </td>
                      <td className="py-2 text-right">
                        <div className="flex justify-end gap-1">
                          <Button
                            variant="ghost"
                            aria-label={`${revealed.has(a.id) ? "Hide" : "Show"} contact details for ${a.name}`}
                            onClick={() =>
                              setRevealed((cur) => {
                                const next = new Set(cur);
                                if (next.has(a.id)) next.delete(a.id);
                                else next.add(a.id);
                                return next;
                              })
                            }
                          >
                            {revealed.has(a.id) ? "Hide" : "Show"}
                          </Button>
                          {canWrite && (
                            <>
                              <Button variant="ghost" onClick={() => setEditing(a.id)}>
                                Edit
                              </Button>
                              <Button variant={a.is_active ? "danger" : "secondary"} onClick={() => setConfirm(a)}>
                                {a.is_active ? "Deactivate" : "Reactivate"}
                              </Button>
                            </>
                          )}
                        </div>
                      </td>
                    </tr>
                  ),
                )}
              </tbody>
            </table>
          </div>
        )}
      </Card>
      {!canWrite && <Unavailable scope="approvers:write" what="Adding or editing approvers" />}
      <ConfirmDialog
        open={confirm !== null}
        title={confirm?.is_active ? `Deactivate ${confirm.name}?` : `Reactivate ${confirm?.name ?? ""}?`}
        confirmLabel={confirm?.is_active ? "Deactivate" : "Reactivate"}
        danger={confirm?.is_active}
        busy={busy}
        onConfirm={() => confirm && void toggleActive(confirm)}
        onCancel={() => setConfirm(null)}
      >
        {confirm?.is_active
          ? "They can no longer cast new votes, and policies that include them cannot accept new requests until they are replaced or reactivated."
          : "They can vote again on requests whose snapshot includes them."}
      </ConfirmDialog>
    </div>
  );
}
