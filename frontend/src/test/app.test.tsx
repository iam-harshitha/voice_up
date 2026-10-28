import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { AuthorizationBanner, StatusBadge } from "../components/ui";
import {
  ALICE,
  BACKUP,
  BOB,
  REQUEST_ID,
  detailRoutes,
  evaluation,
  mockApi,
  renderApp,
  requestDetail,
  session,
} from "./harness";

const signedOut = { status: 401, body: { error: { code: "session_invalid", message: "Not signed in." } } };
const emptyStats = {
  total: 0,
  by_status: { pending: 0, approved: 0, rejected: 0, expired: 0 },
  pending_waiting_for_condition: 0,
  pending_awaiting_reapproval: 0,
  pending_deadline_passed: 0,
  pending_evaluated: 0,
  pending_evaluation_limit: 200,
  evaluation_complete: true,
};
const overviewRoutes = {
  "GET /requests/stats": { status: 200, body: emptyStats },
  "GET /requests": { status: 200, body: { items: [], total: 0, limit: 8, offset: 0 } },
  "GET /audit-events/recent": { status: 200, body: { items: [], limit: 12 } },
};

describe("authentication", () => {
  it("shows the sign-in page when there is no session and never calls data APIs", async () => {
    const api = mockApi({ "GET /auth/session": signedOut });
    renderApp("/requests");
    expect(await screen.findByRole("button", { name: "Sign in" })).toBeInTheDocument();
    expect(api.calls.map((c) => c.path)).toEqual(["/auth/session"]);
  });

  it("signs in and shows the overview with real API data", async () => {
    const api = mockApi({
      "GET /auth/session": signedOut,
      "POST /auth/login": { status: 200, body: session() },
      ...overviewRoutes,
      "GET /requests/stats": { status: 200, body: { ...emptyStats, total: 7, by_status: { pending: 3, approved: 2, rejected: 1, expired: 1 } } },
    });
    renderApp("/");
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("Email"), "ops@example.com");
    await user.type(screen.getByLabelText("Password"), "correct horse battery staple");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("heading", { name: "Overview" })).toBeInTheDocument();
    expect(await screen.findByText("7")).toBeInTheDocument();
    expect(screen.getByText(/SIMULATED VOICE/)).toBeInTheDocument();
    const login = api.calls.find((c) => c.path === "/auth/login")!;
    expect(login.body).toEqual({ email: "ops@example.com", password: "correct horse battery staple" });
  });

  it("shows the backend's generic message for invalid credentials", async () => {
    mockApi({
      "GET /auth/session": signedOut,
      "POST /auth/login": { status: 401, body: { error: { code: "login_failed", message: "Sign-in failed. Check your email and password." } } },
    });
    renderApp("/");
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("Email"), "ops@example.com");
    await user.type(screen.getByLabelText("Password"), "wrong");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Sign-in failed");
    expect(screen.getByLabelText("Password")).toHaveValue("");
  });

  it("returns to sign-in when the session expires mid-use", async () => {
    const api = mockApi({ "GET /auth/session": { status: 200, body: session() }, ...overviewRoutes });
    renderApp("/");
    expect(await screen.findByRole("heading", { name: "Overview" })).toBeInTheDocument();
    api.set("GET /requests/stats", { status: 401, body: { error: { code: "session_invalid", message: "expired" } } });
    api.set("GET /requests", { status: 401, body: { error: { code: "session_invalid", message: "expired" } } });
    await act(async () => {
      await userEvent.setup().click(screen.getByRole("link", { name: "Approval requests" }));
    });
    expect(await screen.findByText(/Your session has ended/)).toBeInTheDocument();
  });

  it("sign-out calls the API with the CSRF token", async () => {
    const api = mockApi({
      "GET /auth/session": { status: 200, body: session() },
      ...overviewRoutes,
      "POST /auth/logout": { status: 204 },
    });
    renderApp("/");
    await userEvent.setup().click(await screen.findByRole("button", { name: "Sign out" }));
    expect(await screen.findByRole("button", { name: "Sign in" })).toBeInTheDocument();
    expect(api.calls.find((c) => c.path === "/auth/logout")!.headers.get("X-CSRF-Token")).toBe("csrf-token-123");
  });

  it("hides actions the user's scopes do not allow", async () => {
    mockApi({
      "GET /auth/session": { status: 200, body: session({ scopes: ["requests:read"] }) },
      ...overviewRoutes,
    });
    renderApp("/policies");
    expect(await screen.findByText(/lacks the/)).toHaveTextContent("policies:read");
  });
});

describe("authorization is never inferred", () => {
  it.each([
    ["pending", evaluation()],
    ["pending", evaluation({ approvals: 2, required_approvals: 2 })], // enough checkmarks, still pending
    ["pending", evaluation({ authorized: true })], // contradictory: pending but authorized flag
    ["rejected", evaluation({ state: "rejected", reason: "veto", next_action: "none" })],
    ["expired", evaluation({ state: "expired", reason: "deadline_passed", next_action: "none" })],
    ["approved", evaluation({ state: "approved", reason: "quorum_met", authorized: false })], // contradictory
    ["pending", evaluation({ state: "waiting_for_condition", reason: "condition_open", condition_approver_ids: [ALICE] })],
    ["pending", evaluation({ state: "waiting_for_condition", reason: "reapproval_required", reapproval_approver_ids: [BOB] })],
  ] as const)("%s with %o is shown as not authorized", (status, ev) => {
    render(<AuthorizationBanner status={status} evaluation={ev} />);
    expect(screen.getByRole("status")).toHaveTextContent("Not authorized");
    expect(screen.queryByText("✓ Authorized")).toBeNull();
  });

  it("shows authorized only for status approved with authorized=true", () => {
    render(
      <AuthorizationBanner
        status="approved"
        evaluation={evaluation({ state: "approved", reason: "quorum_met", authorized: true, next_action: "none" })}
      />,
    );
    expect(screen.getByRole("status")).toHaveTextContent("Authorized");
  });

  it("labels condition and re-approval states in words, not only colour", () => {
    const { rerender } = render(
      <StatusBadge status="pending" evaluation={evaluation({ reason: "condition_open", state: "waiting_for_condition" })} />,
    );
    expect(screen.getByText("Waiting for condition")).toBeInTheDocument();
    rerender(<StatusBadge status="pending" evaluation={evaluation({ reason: "reapproval_required", state: "waiting_for_condition" })} />);
    expect(screen.getByText("Waiting for re-approval")).toBeInTheDocument();
  });
});

describe("request detail", () => {
  it("renders votes, condition, re-approval, calls and an unknown audit event from backend data", async () => {
    const detail = requestDetail({
      votes: [{ id: "v1", approver_id: BACKUP, seat_approver_id: ALICE, vote: "approve", created_at: "2026-10-24T10:01:00Z" }],
      approvers: [
        { approver_id: ALICE, name: "Alice", role: "primary", can_veto: false, position: 0 },
        { approver_id: BOB, name: "Bob", role: "primary", can_veto: false, position: 1 },
        { approver_id: BACKUP, name: "Bea Backup", role: "backup", can_veto: false, position: 2 },
      ],
      evaluation: evaluation({
        state: "waiting_for_condition",
        reason: "reapproval_required",
        approvals: 1,
        outstanding_approver_ids: [],
        reapproval_approver_ids: [BOB],
        reapproval_seat_ids: [BOB],
      }),
    });
    mockApi({
      ...detailRoutes(detail),
      [`GET /requests/${REQUEST_ID}/conditions`]: {
        status: 200,
        body: [
          {
            id: "c1",
            approver_id: BOB,
            seat_approver_id: BOB,
            source: "voice",
            voice_call_id: null,
            description: "only after the database backup is verified",
            status: "satisfied",
            policy_version: 1,
            created_at: "2026-10-24T10:02:00Z",
            closed_at: "2026-10-24T10:03:00Z",
            resolved_by_api_key_id: "k1",
            resolution_note: "backup verified",
            evidence_reference: "CHG-1042",
          },
        ],
      },
      [`GET /requests/${REQUEST_ID}/voice-calls`]: {
        status: 200,
        body: {
          request_id: REQUEST_ID,
          calls: [
            {
              id: "call1", approver_id: ALICE, generation: 1, provider: "mock", status: "completed",
              provider_status: "completed", provider_call_id: "p1", initiation_attempts: 1,
              decision_status: "accepted", decision_candidate: "approve", decision_confidence: 0.95,
              outcome: "vote_recorded", vote_id: "v1", last_error: null, initiated_at: null,
              answered_at: null, ended_at: null, created_at: "2026-10-24T10:00:00Z", updated_at: "2026-10-24T10:01:00Z",
            },
          ],
        },
      },
      [`GET /requests/${REQUEST_ID}/audit-events`]: {
        status: 200,
        body: {
          items: [
            { sequence: 2, request_id: REQUEST_ID, policy_id: null, event_type: "future.event_kind", event_data: { x: 1 }, created_at: "2026-10-24T10:05:00Z" },
            { sequence: 1, request_id: REQUEST_ID, policy_id: null, event_type: "request.created", event_data: {}, created_at: "2026-10-24T10:00:00Z" },
          ],
          limit: 200,
          next_after: null,
        },
      },
    });
    renderApp(`/requests/${REQUEST_ID}`);

    expect(await screen.findByRole("heading", { name: "deploy_to_production" })).toBeInTheDocument();
    expect(screen.getByText("✕ Not authorized")).toBeInTheDocument();
    expect(screen.getByText(/by backup Bea Backup/)).toBeInTheDocument();
    expect(screen.getByText(/Must re-approve unconditionally/).parentElement).toHaveTextContent("Bob");
    expect(await screen.findByText(/only after the database backup is verified/)).toBeInTheDocument();
    expect(screen.getByText(/Satisfied does not approve/)).toBeInTheDocument();
    expect(await screen.findByText("Valid decision recorded as a vote")).toBeInTheDocument();
    expect(screen.getByText("simulated")).toBeInTheDocument();
    const timeline = await screen.findByText("future event kind");
    expect(timeline).toBeInTheDocument();
    const items = within(timeline.closest("ol")!).getAllByRole("listitem");
    expect(items[0]).toHaveTextContent("Request created"); // ordered by sequence
  });

  it("resolving a condition requires confirmation and sends CSRF; it never shows approval", async () => {
    const detail = requestDetail({
      evaluation: evaluation({ state: "waiting_for_condition", reason: "condition_open", next_action: "resolve_condition", condition_approver_ids: [BOB] }),
    });
    const api = mockApi({
      ...detailRoutes(detail),
      [`GET /requests/${REQUEST_ID}/conditions`]: {
        status: 200,
        body: [{
          id: "c1", approver_id: BOB, seat_approver_id: BOB, source: "api", voice_call_id: null,
          description: "after QA", status: "open", policy_version: 1, created_at: "2026-10-24T10:02:00Z",
          closed_at: null, resolved_by_api_key_id: null, resolution_note: null, evidence_reference: null,
        }],
      },
      [`POST /requests/${REQUEST_ID}/conditions/c1/resolve`]: {
        status: 200,
        body: {
          condition: { id: "c1", status: "satisfied" },
          request: { id: REQUEST_ID, status: "pending", evaluation: evaluation({ reason: "reapproval_required" }) },
          created: true,
        },
      },
    });
    renderApp(`/requests/${REQUEST_ID}`);
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("Note"), "QA signed off");
    await user.click(screen.getByRole("button", { name: "Review resolution" }));
    expect(api.calls.some((c) => c.method === "POST")).toBe(false); // not yet: confirmation first
    await user.click(await screen.findByRole("button", { name: "Mark satisfied" }));
    await waitFor(() => expect(api.calls.some((c) => c.path.endsWith("/resolve"))).toBe(true));
    const post = api.calls.find((c) => c.path.endsWith("/resolve"))!;
    expect(post.body).toEqual({ outcome: "satisfied", note: "QA signed off", evidence_reference: null });
    expect(post.headers.get("X-CSRF-Token")).toBe("csrf-token-123");
    expect(await screen.findByText(/still not authorized until Bob re-approves/)).toBeInTheDocument();
  });

  it("polls a pending request and stops once it is final", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let n = 0;
    const api = mockApi(
      detailRoutes(() => {
        n += 1;
        return {
          status: 200,
          body:
            n < 2
              ? requestDetail()
              : requestDetail({
                  status: "approved",
                  decided_at: "2026-10-24T10:10:00Z",
                  evaluation: evaluation({ state: "approved", reason: "quorum_met", authorized: true, next_action: "none", approvals: 2 }),
                }),
        };
      }),
    );
    renderApp(`/requests/${REQUEST_ID}`);
    expect(await screen.findByText("✕ Not authorized")).toBeInTheDocument();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4_500);
    });
    expect(await screen.findByText("✓ Authorized")).toBeInTheDocument();
    const detailCalls = () => api.calls.filter((c) => c.path === `/requests/${REQUEST_ID}`).length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });
    const settled = detailCalls();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(20_000);
    });
    expect(detailCalls()).toBe(settled); // no more polling for a final request
  });

  it("keeps showing data but flags it as stale when a refresh fails", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const api = mockApi(detailRoutes(requestDetail()));
    renderApp(`/requests/${REQUEST_ID}`);
    expect(await screen.findByRole("heading", { name: "deploy_to_production" })).toBeInTheDocument();
    api.set(`GET /requests/${REQUEST_ID}`, { status: 503, body: { error: { code: "unavailable", message: "x" } } });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4_500);
    });
    expect(await screen.findByText(/Refresh failed/)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "deploy_to_production" })).toBeInTheDocument();
  });

  it("shows a not-found error for another organization's request", async () => {
    mockApi({
      "GET /auth/session": { status: 200, body: session() },
      [`GET /requests/${REQUEST_ID}`]: { status: 404, body: { error: { code: "request_not_found", message: "Approval request not found." } } },
    });
    renderApp(`/requests/${REQUEST_ID}`);
    expect(await screen.findByRole("alert")).toHaveTextContent("Not found");
  });

  it("schedules voice calls through the real endpoint and labels mock calls as simulated", async () => {
    const api = mockApi({
      ...detailRoutes(requestDetail()),
      [`POST /requests/${REQUEST_ID}/voice-calls`]: { status: 201, body: { request_id: REQUEST_ID, created: 2, calls: [], skipped: [] } },
    });
    renderApp(`/requests/${REQUEST_ID}`);
    const user = userEvent.setup();
    const button = await screen.findByRole("button", { name: "Schedule calls" });
    await user.click(button);
    expect(await screen.findByText(/2 call\(s\) queued/)).toBeInTheDocument();
    const post = api.calls.find((c) => c.method === "POST")!;
    expect(post.body).toEqual({ redial_unanswered: false });
    expect(screen.getAllByText(/SIMULATED VOICE/).length).toBeGreaterThan(0);
  });
});

describe("requests list", () => {
  it("renders backend evaluations and filters server-side", async () => {
    const api = mockApi({
      "GET /auth/session": { status: 200, body: session() },
      "GET /requests": {
        status: 200,
        body: {
          items: [requestDetail({ evaluation: evaluation({ approvals: 1, condition_approver_ids: [ALICE], reason: "condition_open", state: "waiting_for_condition" }) })],
          total: 1,
          limit: 25,
          offset: 0,
        },
      },
    });
    renderApp("/requests");
    expect(await screen.findByText("deploy_to_production")).toBeInTheDocument();
    expect(screen.getByText("1 / 2")).toBeInTheDocument();
    expect(screen.getByText("Open condition")).toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole("button", { name: "Rejected" }));
    await waitFor(() => {
      const last = vi.mocked(fetch).mock.calls.at(-1)![0] as string;
      expect(last).toContain("status=rejected");
    });
    expect(api.calls.every((c) => c.method === "GET")).toBe(true);
  });

  it("shows an empty state and an error with retry", async () => {
    const api = mockApi({
      "GET /auth/session": { status: 200, body: session() },
      "GET /requests": { status: 500, body: { error: { code: "internal_error", message: "x" } } },
    });
    renderApp("/requests");
    expect(await screen.findByText(/server had a problem/)).toBeInTheDocument();
    api.set("GET /requests", { status: 200, body: { items: [], total: 0, limit: 25, offset: 0 } });
    await userEvent.setup().click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("No requests match this filter.")).toBeInTheDocument();
  });
});

describe("request creation", () => {
  const policy = {
    id: "eeeeeeee-0000-4000-8000-000000000005",
    name: "Production deploys",
    description: null,
    approval_mode: "unanimous",
    required_approvals: 2,
    veto_mode: "any_rejection",
    is_active: true,
    version: 1,
    approvers: [],
    veto_approver_ids: [],
    backup_approvers: [],
    decision_deadline_seconds: null,
    escalation_after_seconds: null,
    max_escalations: 0,
    created_at: "2026-10-24T10:00:00Z",
    updated_at: "2026-10-24T10:00:00Z",
  };

  it("creates once even when the button is clicked twice, then opens the request", async () => {
    let resolveCreate: (value: { status: number; body: unknown }) => void = () => {};
    const api = mockApi({
      ...detailRoutes(requestDetail()),
      "GET /policies": { status: 200, body: { items: [policy], total: 1, limit: 100, offset: 0 } },
      "POST /requests": () => new Promise((resolve) => (resolveCreate = resolve)),
    });
    renderApp("/requests/new");
    const user = userEvent.setup();
    await user.selectOptions(await screen.findByLabelText("Policy"), policy.id);
    await user.click(screen.getByRole("button", { name: "Use the production-deployment example" }));
    const submit = screen.getByRole("button", { name: "Create request" });
    await user.click(submit);
    await user.click(screen.getByRole("button", { name: "Creating…" }));
    expect(api.calls.filter((c) => c.method === "POST")).toHaveLength(1);
    const body = api.calls.find((c) => c.method === "POST")!.body as Record<string, unknown>;
    expect(body.action).toBe("deploy_to_production");
    expect(body.context).toMatchObject({ release: "v2.4", includes_database_migration: true });
    resolveCreate({ status: 201, body: requestDetail() });
    expect(await screen.findByRole("heading", { name: "deploy_to_production" })).toBeInTheDocument();
  });

  it("validates input before submitting", async () => {
    const api = mockApi({
      "GET /auth/session": { status: 200, body: session() },
      "GET /policies": { status: 200, body: { items: [policy], total: 1, limit: 100, offset: 0 } },
    });
    renderApp("/requests/new");
    const user = userEvent.setup();
    await screen.findByLabelText("Policy");
    await user.type(screen.getByLabelText(/Context/), "[[1,2]"); // "[[" types a literal "["
    await user.click(screen.getByRole("button", { name: "Create request" }));
    expect(await screen.findByText("Choose a policy.")).toBeInTheDocument();
    expect(screen.getByText("Action is required.")).toBeInTheDocument();
    expect(screen.getByText(/must be a JSON object/)).toBeInTheDocument();
    expect(api.calls.some((c) => c.method === "POST")).toBe(false);
  });

  it("after an ambiguous failure, resubmitting reuses the same idempotency key", async () => {
    let attempt = 0;
    const api = mockApi({
      ...detailRoutes(requestDetail()),
      "GET /policies": { status: 200, body: { items: [policy], total: 1, limit: 100, offset: 0 } },
      "POST /requests": () => {
        attempt += 1;
        return attempt === 1
          ? { status: 502, body: { error: { code: "bad_gateway", message: "x" } } }
          : { status: 200, body: requestDetail() }; // the server replays the original
      },
    });
    renderApp("/requests/new");
    const user = userEvent.setup();
    await user.selectOptions(await screen.findByLabelText("Policy"), policy.id);
    await user.click(screen.getByRole("button", { name: "Use the production-deployment example" }));
    await user.click(screen.getByRole("button", { name: "Create request" }));
    expect(await screen.findByText(/outcome is unknown/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Create request" }));
    expect(await screen.findByRole("heading", { name: "deploy_to_production" })).toBeInTheDocument();
    const posts = api.calls.filter((c) => c.method === "POST");
    expect(posts).toHaveLength(2);
    const first = posts[0]!.headers.get("Idempotency-Key");
    expect(first).toMatch(/^dash-[0-9a-f-]{36}$/);
    expect(posts[1]!.headers.get("Idempotency-Key")).toBe(first);
  });

  it("uses a new idempotency key once the form content changes", async () => {
    const api = mockApi({
      ...detailRoutes(requestDetail()),
      "GET /policies": { status: 200, body: { items: [policy], total: 1, limit: 100, offset: 0 } },
      "POST /requests": { status: 502, body: { error: { code: "bad_gateway", message: "x" } } },
    });
    renderApp("/requests/new");
    const user = userEvent.setup();
    await user.selectOptions(await screen.findByLabelText("Policy"), policy.id);
    await user.click(screen.getByRole("button", { name: "Use the production-deployment example" }));
    await user.click(screen.getByRole("button", { name: "Create request" }));
    await screen.findByText(/outcome is unknown/);
    await user.type(screen.getByLabelText("Action"), "_v2");
    await user.click(screen.getByRole("button", { name: "Create request" }));
    await waitFor(() => expect(api.calls.filter((c) => c.method === "POST")).toHaveLength(2));
    const [a, b] = api.calls.filter((c) => c.method === "POST");
    expect(a!.headers.get("Idempotency-Key")).not.toBe(b!.headers.get("Idempotency-Key"));
  });

});
