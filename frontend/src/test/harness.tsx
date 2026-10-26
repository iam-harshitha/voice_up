import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";
import { App } from "../App";
import { setCsrfToken } from "../api/client";
import type { Evaluation, RequestDetail, SessionInfo } from "../api/types";
import { AuthProvider } from "../auth/AuthContext";
import { ToastProvider } from "../components/ui";

export type Handler = (request: { method: string; url: URL; body: unknown; headers: Headers }) =>
  | { status: number; body?: unknown }
  | Promise<{ status: number; body?: unknown }>;

export interface MockApi {
  calls: { method: string; path: string; body: unknown; headers: Headers }[];
  set: (route: string, handler: Handler | { status: number; body?: unknown }) => void;
}

/** Routes are "METHOD /path" relative to /api/v1 (query strings ignored). */
export function mockApi(routes: Record<string, Handler | { status: number; body?: unknown }> = {}): MockApi {
  const table = new Map(Object.entries(routes));
  const calls: MockApi["calls"] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = new URL(String(input), "http://localhost");
    const method = (init?.method ?? "GET").toUpperCase();
    const path = url.pathname.replace(/^\/api\/v1/, "");
    const headers = new Headers(init?.headers);
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ method, path, body, headers });
    const route = table.get(`${method} ${path}`);
    if (!route) {
      return new Response(JSON.stringify({ error: { code: "not_found", message: `No mock for ${method} ${path}` } }), {
        status: 404,
      });
    }
    const result = typeof route === "function" ? await route({ method, url, body, headers }) : route;
    return new Response(result.body === undefined ? null : JSON.stringify(result.body), { status: result.status });
  });
  return { calls, set: (route, handler) => table.set(route, handler) };
}

export const ALL_SCOPES = [
  "approvers:read",
  "approvers:write",
  "audit:read",
  "conditions:resolve",
  "policies:read",
  "policies:write",
  "requests:create",
  "requests:read",
  "voice:calls:create",
  "voice:calls:read",
];

export function session(overrides: Partial<SessionInfo> = {}): SessionInfo {
  return {
    user: { id: "u1", email: "ops@example.com", display_name: "Ops Lead" },
    organization: { id: "o1", name: "Acme" },
    scopes: ALL_SCOPES,
    csrf_token: "csrf-token-123",
    expires_at: "2099-01-01T00:00:00Z",
    environment: { app_env: "development", voice_provider: "mock" },
    ...overrides,
  };
}

export const ALICE = "aaaaaaaa-0000-4000-8000-000000000001";
export const BOB = "bbbbbbbb-0000-4000-8000-000000000002";
export const BACKUP = "cccccccc-0000-4000-8000-000000000003";
export const REQUEST_ID = "dddddddd-0000-4000-8000-000000000004";

export function evaluation(overrides: Partial<Evaluation> = {}): Evaluation {
  return {
    state: "awaiting_approvals",
    reason: "awaiting_decisions",
    authorized: false,
    next_action: "await_decisions",
    approvals: 0,
    rejections: 0,
    required_approvals: 2,
    veto_triggered: false,
    outstanding_approver_ids: [ALICE, BOB],
    condition_approver_ids: [],
    reapproval_approver_ids: [],
    reapproval_seat_ids: [],
    deadline_passed: false,
    expires_at: null,
    ...overrides,
  };
}

export function requestDetail(overrides: Partial<RequestDetail> = {}): RequestDetail {
  return {
    id: REQUEST_ID,
    policy_id: "eeeeeeee-0000-4000-8000-000000000005",
    policy_version: 1,
    action: "deploy_to_production",
    summary: "Deploy release v2.4 to production.",
    status: "pending",
    required_approvals: 2,
    created_at: "2026-10-24T10:00:00Z",
    updated_at: "2026-10-24T10:00:00Z",
    decided_at: null,
    expires_at: null,
    evaluation: evaluation(),
    context: { release: "v2.4" },
    authorized_approver_ids: [ALICE, BOB],
    votes: [],
    policy: {
      schema_version: 2,
      policy_id: "eeeeeeee-0000-4000-8000-000000000005",
      name: "Production deploys",
      version: 1,
      approval_mode: "unanimous",
      required_approvals: 2,
      veto_mode: "any_rejection",
      deadline_seconds: null,
      escalation_after_seconds: null,
      max_escalations: 0,
    },
    approvers: [
      { approver_id: ALICE, name: "Alice", role: "primary", can_veto: false, position: 0 },
      { approver_id: BOB, name: "Bob", role: "primary", can_veto: false, position: 1 },
    ],
    ...overrides,
  };
}

/** Routes the request detail page loads, with sensible empty defaults. */
export function detailRoutes(detail: RequestDetail | Handler) {
  return {
    "GET /auth/session": { status: 200, body: session() },
    [`GET /requests/${REQUEST_ID}`]: typeof detail === "function" ? detail : { status: 200, body: detail },
    [`GET /requests/${REQUEST_ID}/conditions`]: { status: 200, body: [] },
    [`GET /requests/${REQUEST_ID}/escalations`]: { status: 200, body: [] },
    [`GET /requests/${REQUEST_ID}/voice-calls`]: { status: 200, body: { request_id: REQUEST_ID, calls: [] } },
    [`GET /requests/${REQUEST_ID}/audit-events`]: { status: 200, body: { items: [], limit: 200, next_after: null } },
  };
}

export function renderApp(path = "/") {
  setCsrfToken(null);
  return render(
    <MemoryRouter initialEntries={[path]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <AuthProvider>
        <ToastProvider>
          <App />
        </ToastProvider>
      </AuthProvider>
    </MemoryRouter>,
  );
}

export function renderWithProviders(ui: ReactElement, path = "/") {
  return render(
    <MemoryRouter initialEntries={[path]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <AuthProvider>
        <ToastProvider>{ui}</ToastProvider>
      </AuthProvider>
    </MemoryRouter>,
  );
}
