import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useId,
  useRef,
  useState,
  type ButtonHTMLAttributes,
  type ReactNode,
} from "react";
import type { ApiError } from "../api/client";
import type { Evaluation, RequestStatus } from "../api/types";
import { formatRelative, workflowLabel } from "../lib/format";

export function cx(...classes: (string | false | null | undefined)[]): string {
  return classes.filter(Boolean).join(" ");
}

// --- Buttons and cards -------------------------------------------------------------------

type Variant = "primary" | "secondary" | "danger" | "ghost";
const VARIANTS: Record<Variant, string> = {
  primary: "bg-slate-900 text-white hover:bg-slate-700 disabled:bg-slate-400",
  secondary: "bg-white text-slate-800 ring-1 ring-inset ring-slate-300 hover:bg-slate-50 disabled:text-slate-400",
  danger: "bg-red-700 text-white hover:bg-red-600 disabled:bg-red-300",
  ghost: "text-slate-700 hover:bg-slate-100 disabled:text-slate-400",
};

export function Button({
  variant = "secondary",
  className,
  type = "button",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant }) {
  return (
    <button
      type={type}
      className={cx(
        "inline-flex items-center justify-center gap-2 rounded-md px-3 py-1.5 text-sm font-medium",
        "focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-600 focus-visible:ring-offset-2",
        "disabled:cursor-not-allowed",
        VARIANTS[variant],
        className,
      )}
      {...props}
    />
  );
}

export function Card({
  title,
  actions,
  children,
  className,
}: {
  title?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={cx("rounded-lg border border-slate-200 bg-white shadow-sm", className)}>
      {(title || actions) && (
        <header className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-100 px-4 py-3">
          {title && <h2 className="text-sm font-semibold text-slate-900">{title}</h2>}
          {actions && <div className="flex items-center gap-2">{actions}</div>}
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  );
}

// --- Status --------------------------------------------------------------------------------

const STATUS_STYLE: Record<string, { cls: string; icon: string }> = {
  approved: { cls: "bg-emerald-50 text-emerald-800 ring-emerald-600/30", icon: "✓" },
  rejected: { cls: "bg-red-50 text-red-800 ring-red-600/30", icon: "✕" },
  expired: { cls: "bg-slate-100 text-slate-700 ring-slate-500/30", icon: "⌛" },
  condition: { cls: "bg-amber-50 text-amber-900 ring-amber-600/30", icon: "!" },
  pending: { cls: "bg-sky-50 text-sky-800 ring-sky-600/30", icon: "…" },
};

/** Request status as reported by the backend; the label is always written out. */
export function StatusBadge({ status, evaluation }: { status: RequestStatus; evaluation: Evaluation | null }) {
  const blocked =
    status === "pending" &&
    evaluation !== null &&
    (evaluation.reason === "condition_open" || evaluation.reason === "reapproval_required");
  const style = STATUS_STYLE[blocked ? "condition" : status] ?? STATUS_STYLE.pending!;
  return (
    <span
      className={cx(
        "inline-flex items-center gap-1 whitespace-nowrap rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset",
        style.cls,
      )}
    >
      <span aria-hidden="true">{style.icon}</span>
      {workflowLabel(status, evaluation)}
    </span>
  );
}

/**
 * Whether the action may proceed. Shown as authorized ONLY when the backend says
 * authorized === true AND status === "approved". Anything else is "not authorized".
 */
export function AuthorizationBanner({ status, evaluation }: { status: RequestStatus; evaluation: Evaluation | null }) {
  const authorized = evaluation?.authorized === true && status === "approved";
  if (authorized) {
    return (
      <div role="status" className="rounded-md border border-emerald-300 bg-emerald-50 px-4 py-3 text-emerald-900">
        <p className="font-semibold">✓ Authorized</p>
        <p className="text-sm">The policy is satisfied. The agent may perform exactly this action.</p>
      </div>
    );
  }
  const final = status === "rejected" || status === "expired";
  return (
    <div
      role="status"
      className={cx(
        "rounded-md border px-4 py-3",
        final ? "border-red-300 bg-red-50 text-red-900" : "border-slate-300 bg-slate-50 text-slate-900",
      )}
    >
      <p className="font-semibold">✕ Not authorized</p>
      <p className="text-sm">
        {final
          ? `The request is ${status}. The action must not be performed.`
          : "The policy is not yet satisfied. The action must not be performed yet."}
      </p>
    </div>
  );
}

// --- Loading, error, empty -------------------------------------------------------------

export function Spinner({ label = "Loading" }: { label?: string }) {
  return (
    <span role="status" className="inline-flex items-center gap-2 text-sm text-slate-500">
      <span className="h-3 w-3 animate-spin rounded-full border-2 border-slate-300 border-t-slate-700" aria-hidden="true" />
      {label}…
    </span>
  );
}

export function LoadingBlock({ label }: { label?: string }) {
  return (
    <div className="flex min-h-[6rem] items-center justify-center">
      <Spinner label={label} />
    </div>
  );
}

export function ErrorState({ error, onRetry, compact }: { error: ApiError; onRetry?: () => void; compact?: boolean }) {
  return (
    <div role="alert" className={cx("rounded-md border border-red-200 bg-red-50 text-red-900", compact ? "px-3 py-2" : "p-4")}>
      <p className="text-sm font-medium">{error.userMessage}</p>
      {error.requestId && <p className="mt-1 text-xs text-red-700">Reference: {error.requestId}</p>}
      {onRetry && (
        <Button className="mt-2" onClick={onRetry}>
          Retry
        </Button>
      )}
    </div>
  );
}

export function StaleNotice({ lastUpdated, onRetry }: { lastUpdated: Date | null; onRetry: () => void }) {
  return (
    <div role="alert" className="flex flex-wrap items-center gap-2 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900">
      <span>
        Refresh failed. Showing data from {lastUpdated ? formatRelative(lastUpdated.toISOString()) : "earlier"}; it may be out of date.
      </span>
      <Button variant="ghost" onClick={onRetry}>
        Retry now
      </Button>
    </div>
  );
}

export function EmptyState({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="rounded-md border border-dashed border-slate-300 px-4 py-8 text-center">
      <p className="text-sm font-medium text-slate-700">{title}</p>
      {children && <div className="mt-1 text-sm text-slate-500">{children}</div>}
    </div>
  );
}

export function Unavailable({ scope, what }: { scope: string; what: string }) {
  return (
    <p className="text-sm text-slate-500">
      {what} is unavailable: your account lacks the <code className="rounded bg-slate-100 px-1">{scope}</code> scope.
    </p>
  );
}

// --- Form fields ----------------------------------------------------------------------------

export function Field({
  label,
  hint,
  error,
  children,
}: {
  label: string;
  hint?: string;
  error?: string | null;
  children: (props: { id: string; "aria-describedby"?: string; "aria-invalid"?: boolean }) => ReactNode;
}) {
  const id = useId();
  const describedBy = [hint ? `${id}-hint` : null, error ? `${id}-error` : null].filter(Boolean).join(" ") || undefined;
  return (
    <div className="space-y-1">
      <label htmlFor={id} className="block text-sm font-medium text-slate-800">
        {label}
      </label>
      {children({ id, "aria-describedby": describedBy, "aria-invalid": error ? true : undefined })}
      {hint && (
        <p id={`${id}-hint`} className="text-xs text-slate-500">
          {hint}
        </p>
      )}
      {error && (
        <p id={`${id}-error`} className="text-xs font-medium text-red-700">
          {error}
        </p>
      )}
    </div>
  );
}

export const inputClass =
  "block w-full rounded-md border-0 px-2.5 py-1.5 text-sm text-slate-900 ring-1 ring-inset ring-slate-300 " +
  "placeholder:text-slate-400 focus:ring-2 focus:ring-inset focus:ring-sky-600 aria-[invalid=true]:ring-red-500";

// --- Confirmation dialog -------------------------------------------------------------------

export function ConfirmDialog({
  open,
  title,
  children,
  confirmLabel,
  danger,
  busy,
  onConfirm,
  onCancel,
}: {
  open: boolean;
  title: string;
  children: ReactNode;
  confirmLabel: string;
  danger?: boolean;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) {
      // Real browsers: a modal dialog with focus trapping and Escape handling.
      if (typeof dialog.showModal === "function") dialog.showModal();
      else dialog.setAttribute("open", "");
    }
  }, [open]);
  if (!open) return null;
  return (
    <dialog
      ref={ref}
      aria-labelledby="confirm-title"
      className="fixed inset-0 z-50 m-auto w-full max-w-md rounded-lg border border-slate-200 bg-white p-0 shadow-xl backdrop:bg-slate-900/40"
      onCancel={(event) => {
        event.preventDefault();
        if (!busy) onCancel();
      }}
    >
      <div className="p-5">
        <h2 id="confirm-title" className="text-base font-semibold text-slate-900">
          {title}
        </h2>
        <div className="mt-2 text-sm text-slate-700">{children}</div>
        <div className="mt-5 flex justify-end gap-2">
          <Button onClick={onCancel} disabled={busy}>
            Cancel
          </Button>
          <Button variant={danger ? "danger" : "primary"} onClick={onConfirm} disabled={busy}>
            {busy ? "Working…" : confirmLabel}
          </Button>
        </div>
      </div>
    </dialog>
  );
}

// --- Toasts -----------------------------------------------------------------------------------

interface Toast {
  id: number;
  kind: "success" | "error" | "info";
  text: string;
}
const ToastContext = createContext<(kind: Toast["kind"], text: string) => void>(() => {});

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const nextId = useRef(1);
  const push = useCallback((kind: Toast["kind"], text: string) => {
    const id = nextId.current++;
    setToasts((current) => [...current, { id, kind, text }]);
    setTimeout(() => setToasts((current) => current.filter((t) => t.id !== id)), 6000);
  }, []);
  return (
    <ToastContext.Provider value={push}>
      {children}
      <div aria-live="polite" className="fixed bottom-4 right-4 z-50 flex w-80 flex-col gap-2">
        {toasts.map((toast) => (
          <div
            key={toast.id}
            role={toast.kind === "error" ? "alert" : "status"}
            className={cx(
              "rounded-md border px-3 py-2 text-sm shadow-md",
              toast.kind === "success" && "border-emerald-300 bg-emerald-50 text-emerald-900",
              toast.kind === "error" && "border-red-300 bg-red-50 text-red-900",
              toast.kind === "info" && "border-slate-300 bg-white text-slate-900",
            )}
          >
            {toast.text}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast() {
  return useContext(ToastContext);
}

export function LastUpdated({ at, polling }: { at: Date | null; polling: boolean }) {
  if (!at) return null;
  return (
    <span className="text-xs text-slate-500">
      Updated {at.toLocaleTimeString()} {polling ? "· refreshing automatically" : ""}
    </span>
  );
}
