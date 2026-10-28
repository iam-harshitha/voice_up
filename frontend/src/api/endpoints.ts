// Typed wrappers for the backend endpoints the dashboard uses. Paths and payloads follow
// the backend exactly (see backend/README.md and backend/docs/dashboard-mcp.md).

import { apiRequest } from "./client";
import type {
  Approver,
  AuditEvent,
  AuditTimeline,
  Condition,
  ConditionResult,
  EscalationStep,
  Page,
  Policy,
  PolicyInput,
  RequestDetail,
  RequestStats,
  RequestStatus,
  RequestSummary,
  SessionInfo,
  VoiceCall,
  VoiceScheduleResult,
} from "./types";

type Signal = { signal?: AbortSignal };

export const api = {
  // --- session -----------------------------------------------------------------------
  login: (email: string, password: string) =>
    apiRequest<SessionInfo>("POST", "/auth/login", { body: { email, password } }),
  session: (o: Signal = {}) => apiRequest<SessionInfo>("GET", "/auth/session", o),
  logout: () => apiRequest<void>("POST", "/auth/logout"),

  // --- requests ----------------------------------------------------------------------
  stats: (o: Signal = {}) => apiRequest<RequestStats>("GET", "/requests/stats", o),
  listRequests: (
    params: { status?: RequestStatus | null; limit: number; offset: number },
    o: Signal = {},
  ) =>
    apiRequest<Page<RequestSummary>>("GET", "/requests", {
      ...o,
      query: { ...params, include_evaluation: true },
    }),
  getRequest: (id: string, o: Signal = {}) => apiRequest<RequestDetail>("GET", `/requests/${id}`, o),
  /** The same key + payload returns the original request (no duplicate) on retry. */
  createRequest: (
    body: { policy_id: string; action: string; summary: string; context: Record<string, unknown> },
    idempotencyKey: string,
  ) => apiRequest<RequestDetail>("POST", "/requests", { body, headers: { "Idempotency-Key": idempotencyKey } }),

  // --- workflow ----------------------------------------------------------------------
  listConditions: (id: string, o: Signal = {}) =>
    apiRequest<Condition[]>("GET", `/requests/${id}/conditions`, o),
  resolveCondition: (
    requestId: string,
    conditionId: string,
    body: { outcome: "satisfied" | "not_satisfied"; note: string; evidence_reference?: string | null },
  ) =>
    apiRequest<ConditionResult>("POST", `/requests/${requestId}/conditions/${conditionId}/resolve`, {
      body,
    }),
  listEscalations: (id: string, o: Signal = {}) =>
    apiRequest<EscalationStep[]>("GET", `/requests/${id}/escalations`, o),
  listVoiceCalls: (id: string, o: Signal = {}) =>
    apiRequest<{ request_id: string; calls: VoiceCall[] }>("GET", `/requests/${id}/voice-calls`, o),
  scheduleVoiceCalls: (id: string, redialUnanswered = false) =>
    apiRequest<VoiceScheduleResult>("POST", `/requests/${id}/voice-calls`, {
      body: { redial_unanswered: redialUnanswered },
    }),
  requestTimeline: (id: string, after: number, o: Signal = {}) =>
    apiRequest<AuditTimeline>("GET", `/requests/${id}/audit-events`, { ...o, query: { after, limit: 200 } }),
  recentEvents: (limit: number, o: Signal = {}) =>
    apiRequest<{ items: AuditEvent[]; limit: number }>("GET", "/audit-events/recent", {
      ...o,
      query: { limit },
    }),

  // --- policies ----------------------------------------------------------------------
  listPolicies: (o: Signal = {}) =>
    apiRequest<Page<Policy>>("GET", "/policies", { ...o, query: { limit: 100, offset: 0 } }),
  getPolicy: (id: string, o: Signal = {}) => apiRequest<Policy>("GET", `/policies/${id}`, o),
  createPolicy: (body: PolicyInput) => apiRequest<Policy>("POST", "/policies", { body }),
  updatePolicy: (id: string, body: Partial<PolicyInput> & { is_active?: boolean }) =>
    apiRequest<Policy>("PATCH", `/policies/${id}`, { body }),
  policyTimeline: (id: string, o: Signal = {}) =>
    apiRequest<AuditTimeline>("GET", `/policies/${id}/audit-events`, { ...o, query: { limit: 50 } }),

  // --- approvers ---------------------------------------------------------------------
  listApprovers: (o: Signal = {}) =>
    apiRequest<Page<Approver>>("GET", "/approvers", { ...o, query: { limit: 100, offset: 0 } }),
  createApprover: (body: { name: string; phone_number?: string | null; email?: string | null }) =>
    apiRequest<Approver>("POST", "/approvers", { body }),
  updateApprover: (
    id: string,
    body: { name?: string; phone_number?: string | null; email?: string | null; is_active?: boolean },
  ) => apiRequest<Approver>("PATCH", `/approvers/${id}`, { body }),
};
