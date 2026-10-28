import type { Evaluation, RequestStatus } from "../api/types";

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function formatRelative(value: string | null | undefined, now: number = Date.now()): string {
  if (!value) return "—";
  const diff = new Date(value).getTime() - now;
  const abs = Math.abs(diff);
  const units: [number, string][] = [
    [86_400_000, "day"],
    [3_600_000, "hour"],
    [60_000, "minute"],
  ];
  for (const [ms, unit] of units) {
    if (abs >= ms) {
      const n = Math.round(abs / ms);
      return diff < 0 ? `${n} ${unit}${n === 1 ? "" : "s"} ago` : `in ${n} ${unit}${n === 1 ? "" : "s"}`;
    }
  }
  return diff < 0 ? "just now" : "in under a minute";
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "None";
  if (seconds % 86_400 === 0) return `${seconds / 86_400} day${seconds === 86_400 ? "" : "s"}`;
  if (seconds % 3600 === 0) return `${seconds / 3600} hour${seconds === 3600 ? "" : "s"}`;
  if (seconds % 60 === 0) return `${seconds / 60} minute${seconds === 60 ? "" : "s"}`;
  return `${seconds} seconds`;
}

export function shortId(id: string): string {
  return id.slice(0, 8);
}

export const STATUS_LABEL: Record<RequestStatus, string> = {
  pending: "Pending",
  approved: "Approved",
  rejected: "Rejected",
  expired: "Expired",
};

const STATE_LABEL: Record<string, string> = {
  awaiting_approvals: "Awaiting decisions",
  waiting_for_condition: "Waiting on a condition",
  approved: "Approved",
  rejected: "Rejected",
  expired: "Expired",
};

const REASON_LABEL: Record<string, string> = {
  quorum_met: "Required approvals reached",
  veto: "Vetoed by a rejection",
  quorum_unreachable: "Required approvals can no longer be reached",
  deadline_passed: "Decision deadline passed",
  condition_open: "An approver set a condition that must be resolved",
  reapproval_required: "Condition satisfied; the approver must re-approve",
  awaiting_decisions: "Waiting for approvers to decide",
};

const NEXT_ACTION_LABEL: Record<string, string> = {
  await_decisions: "Wait for approver decisions",
  resolve_condition: "An authorized person must resolve the open condition",
  none: "None — the decision is final",
};

export function stateLabel(state: string): string {
  return STATE_LABEL[state] ?? state;
}
export function reasonLabel(reason: string): string {
  return REASON_LABEL[reason] ?? reason;
}
export function nextActionLabel(action: string): string {
  return NEXT_ACTION_LABEL[action] ?? action;
}

/** The workflow situation for display, taken from the backend evaluation only. */
export function workflowLabel(status: RequestStatus, evaluation: Evaluation | null): string {
  if (!evaluation) return STATUS_LABEL[status];
  if (status === "pending") {
    if (evaluation.deadline_passed) return "Deadline passed (expiring)";
    if (evaluation.reason === "condition_open") return "Waiting for condition";
    if (evaluation.reason === "reapproval_required") return "Waiting for re-approval";
    return "Awaiting decisions";
  }
  return STATUS_LABEL[status];
}

const EVENT_LABEL: Record<string, string> = {
  "request.created": "Request created",
  "request.approved": "Request approved",
  "request.rejected": "Request rejected",
  "request.expired": "Request expired",
  "vote.recorded": "Vote recorded",
  "condition.created": "Condition set",
  "condition.resolved": "Condition resolved",
  "condition.superseded": "Condition superseded by a rejection",
  "condition.lapsed": "Condition lapsed",
  "condition.reapproval_requested": "Re-approval requested",
  "escalation.step_created": "Escalated to a backup approver",
  "escalation.exhausted": "Escalation chain exhausted",
  "policy.created": "Policy created",
  "policy.updated": "Policy updated",
  "voice.call_scheduled": "Call scheduled",
  "voice.call_initiation_attempted": "Call initiation attempted",
  "voice.call_initiated": "Call initiated",
  "voice.event_ignored": "Provider event ignored",
  "voice.call_failed": "Call failed",
  "voice.decision_accepted": "Spoken decision accepted",
  "voice.decision_rejected": "Spoken answer not accepted as a decision",
};

export function eventLabel(type: string): string {
  return EVENT_LABEL[type] ?? type.replace(/[._]/g, " ");
}

/** Event categories drive the timeline marker; unknown types are "other". */
export function eventTone(type: string): "good" | "bad" | "warn" | "info" | "other" {
  if (type === "request.approved") return "good";
  if (type === "request.rejected" || type === "request.expired" || type === "voice.call_failed") return "bad";
  if (type.startsWith("condition.") || type.startsWith("escalation.") || type === "voice.decision_rejected")
    return "warn";
  if (type.startsWith("request.") || type.startsWith("vote.") || type.startsWith("voice.")) return "info";
  return "other";
}

const CALL_STATUS_LABEL: Record<string, string> = {
  queued: "Queued",
  initiating: "Initiating",
  initiated: "Initiated",
  ringing: "Ringing",
  in_progress: "In progress",
  processing: "Call ended; processing result",
  completed: "Completed",
  failed: "Failed",
  timed_out: "Timed out",
  canceled: "Canceled",
  unknown: "Uncertain (not re-dialled automatically)",
};
const DECISION_STATUS_LABEL: Record<string, string> = {
  awaiting: "No answer yet",
  accepted: "Valid decision recorded as a vote",
  no_decision: "No explicit decision",
  refused: "Decision refused by policy rules",
  conditional: "Conditional approval recorded (not a vote)",
};

export function callStatusLabel(status: string): string {
  return CALL_STATUS_LABEL[status] ?? status;
}
export function decisionStatusLabel(status: string): string {
  return DECISION_STATUS_LABEL[status] ?? status;
}

/** Shows enough of a phone number to recognise it without displaying all of it. */
export function maskPhone(phone: string | null): string {
  if (!phone) return "—";
  return phone.length <= 6 ? "••••" : `${phone.slice(0, 3)}•••${phone.slice(-4)}`;
}

export function maskEmail(email: string | null): string {
  if (!email) return "—";
  const [user, domain] = email.split("@");
  if (!user || !domain) return "•••";
  return `${user.slice(0, 1)}•••@${domain}`;
}
