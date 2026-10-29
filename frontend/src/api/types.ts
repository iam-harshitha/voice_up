// Types mirroring the backend's response schemas (backend/app/schemas). The dashboard
// renders these as returned; it never derives a decision from them.

export type RequestStatus = "pending" | "approved" | "rejected" | "expired";

export interface ApiErrorBody {
  error: {
    code: string;
    message: string;
    request_id?: string | null;
    details?: { loc: (string | number)[]; msg: string; type: string }[] | null;
  };
}

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface SessionInfo {
  user: { id: string; email: string; display_name: string };
  organization: { id: string; name: string };
  scopes: string[];
  csrf_token: string;
  expires_at: string;
  environment: { app_env: string; voice_provider: "disabled" | "mock" | "bolna" | string };
}

export interface Evaluation {
  state: string;
  reason: string;
  authorized: boolean;
  next_action: string;
  approvals: number;
  rejections: number;
  required_approvals: number;
  veto_triggered: boolean;
  outstanding_approver_ids: string[];
  condition_approver_ids: string[];
  reapproval_approver_ids: string[];
  reapproval_seat_ids: string[];
  deadline_passed: boolean;
  expires_at: string | null;
}

export interface Vote {
  id: string;
  approver_id: string;
  seat_approver_id: string;
  vote: "approve" | "reject";
  created_at: string;
}

export interface PolicySnapshot {
  schema_version: number;
  policy_id: string;
  name: string;
  version: number;
  approval_mode: string;
  required_approvals: number;
  veto_mode: string;
  deadline_seconds: number | null;
  escalation_after_seconds: number | null;
  max_escalations: number;
}

export interface RequestApprover {
  approver_id: string;
  name: string;
  role: "primary" | "backup" | string;
  can_veto: boolean;
  position: number;
}

export interface RequestSummary {
  id: string;
  policy_id: string;
  policy_version: number;
  action: string;
  summary: string;
  status: RequestStatus;
  required_approvals: number;
  created_at: string;
  updated_at: string;
  decided_at: string | null;
  expires_at: string | null;
  evaluation: Evaluation | null;
}

export interface RequestDetail extends RequestSummary {
  context: Record<string, unknown>;
  authorized_approver_ids: string[];
  votes: Vote[];
  policy: PolicySnapshot | null;
  approvers: RequestApprover[];
}

export interface RequestStats {
  total: number;
  by_status: Record<RequestStatus, number>;
  pending_waiting_for_condition: number;
  pending_awaiting_reapproval: number;
  pending_deadline_passed: number;
  pending_evaluated: number;
  pending_evaluation_limit: number;
  evaluation_complete: boolean;
}

export interface Condition {
  id: string;
  approver_id: string;
  seat_approver_id: string;
  source: string;
  voice_call_id: string | null;
  description: string;
  status: "open" | "satisfied" | "not_satisfied" | "superseded" | "lapsed" | string;
  policy_version: number;
  created_at: string;
  closed_at: string | null;
  resolved_by_api_key_id: string | null;
  resolution_note: string | null;
  evidence_reference: string | null;
}

export interface ConditionResult {
  condition: Condition;
  request: { id: string; status: RequestStatus; evaluation: Evaluation };
  created: boolean;
}

export interface EscalationStep {
  id: string;
  sequence: number;
  round: number;
  seat_approver_id: string;
  backup_approver_id: string;
  voice_call_id: string | null;
  outcome: string;
  created_at: string;
}

export interface VoiceCall {
  id: string;
  approver_id: string;
  generation: number;
  provider: string;
  status: string;
  provider_status: string | null;
  provider_call_id: string | null;
  initiation_attempts: number;
  decision_status: string;
  decision_candidate: string | null;
  decision_confidence: number | null;
  outcome: string | null;
  vote_id: string | null;
  last_error: string | null;
  initiated_at: string | null;
  answered_at: string | null;
  ended_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface VoiceScheduleResult {
  request_id: string;
  created: number;
  calls: VoiceCall[];
  skipped: { approver_id: string; reason: string }[];
}

export interface AuditEvent {
  sequence: number;
  request_id: string | null;
  policy_id: string | null;
  event_type: string;
  event_data: Record<string, unknown>;
  created_at: string;
}

export interface AuditTimeline {
  items: AuditEvent[];
  limit: number;
  next_after: number | null;
}

export interface Approver {
  id: string;
  name: string;
  phone_number: string | null;
  email: string | null;
  is_active: boolean;
  created_at: string;
  updated_at?: string;
}

export interface PolicyApproverRef {
  id: string;
  name: string;
  is_active: boolean;
}

export interface Policy {
  id: string;
  name: string;
  description: string | null;
  approval_mode: "unanimous" | "quorum" | string;
  required_approvals: number;
  veto_mode: "any_rejection" | "designated" | "none" | string;
  is_active: boolean;
  version: number;
  approvers: PolicyApproverRef[];
  veto_approver_ids: string[];
  backup_approvers: PolicyApproverRef[];
  decision_deadline_seconds: number | null;
  escalation_after_seconds: number | null;
  max_escalations: number;
  auto_dial: boolean;
  created_at: string;
  updated_at: string;
}

export interface PolicyInput {
  name: string;
  description?: string | null;
  approval_mode: "unanimous" | "quorum";
  required_approvals: 1 | 2;
  approver_ids: string[];
  veto_mode: "any_rejection" | "designated" | "none";
  veto_approver_ids: string[];
  backup_approver_ids: string[];
  decision_deadline_seconds: number | null;
  escalation_after_seconds: number | null;
  max_escalations?: number | null;
  auto_dial: boolean;
}
