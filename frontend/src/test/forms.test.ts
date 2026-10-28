import { describe, expect, it } from "vitest";
import { validateApproverForm } from "../pages/ApproversPage";
import { validateRequestForm } from "../pages/NewRequestPage";
import { EMPTY_POLICY, validatePolicyForm, type PolicyFormValues } from "../pages/PoliciesPage";

const A = "a1";
const B = "b2";
const C = "c3";
const policy = (overrides: Partial<PolicyFormValues>): PolicyFormValues => ({ ...EMPTY_POLICY, name: "P", ...overrides });

describe("policy form validation", () => {
  it("accepts a valid unanimous two-person policy", () => {
    const { errors, payload } = validatePolicyForm(policy({ approver_ids: [A, B], required_approvals: 2 }));
    expect(errors).toEqual({});
    expect(payload).toMatchObject({ approval_mode: "unanimous", required_approvals: 2, approver_ids: [A, B] });
  });

  it.each([
    [{ approver_ids: [] }, "approver_ids"],
    [{ approver_ids: [A, B], required_approvals: 1 as const }, "required_approvals"],
    [{ approver_ids: [A], veto_mode: "none" as const }, "veto_mode"],
    [{ approval_mode: "quorum" as const, approver_ids: [A], required_approvals: 2 as const }, "required_approvals"],
    [{ approval_mode: "quorum" as const, approver_ids: [A, B], veto_mode: "designated" as const }, "veto_approver_ids"],
    [{ approver_ids: [A], backup_approver_ids: [A] }, "backup_approver_ids"],
    [{ approver_ids: [A], escalation_minutes: "10" }, "escalation_minutes"],
    [{ approver_ids: [A], backup_approver_ids: [B], escalation_minutes: "10", deadline_minutes: "10" }, "escalation_minutes"],
    [{ approver_ids: [A], deadline_minutes: "0.5" }, "deadline_minutes"],
    [{ approver_ids: [A], backup_approver_ids: [B], escalation_minutes: "10", max_escalations: "2" }, "max_escalations"],
    [{ approver_ids: [A], max_escalations: "1" }, "max_escalations"],
    [{ name: " " , approver_ids: [A] }, "name"],
  ])("rejects %o (%s)", (overrides, field) => {
    const { errors, payload } = validatePolicyForm(policy(overrides));
    expect(errors[field]).toBeTruthy();
    expect(payload).toBeNull();
  });

  it("converts minutes to seconds and keeps veto holders only for designated mode", () => {
    const { payload } = validatePolicyForm(
      policy({
        approval_mode: "quorum",
        approver_ids: [A, B, C],
        required_approvals: 2,
        veto_mode: "designated",
        veto_approver_ids: [A],
        deadline_minutes: "60",
      }),
    );
    expect(payload).toMatchObject({ decision_deadline_seconds: 3600, veto_approver_ids: [A], escalation_after_seconds: null });
  });
});

describe("approver form validation", () => {
  it("requires a contact and validates formats", () => {
    expect(validateApproverForm({ name: "Ann", phone: "", email: "" }).errors.phone).toBeTruthy();
    expect(validateApproverForm({ name: "Ann", phone: "4155550101", email: "" }).errors.phone).toMatch(/E.164/);
    expect(validateApproverForm({ name: "Ann", phone: "", email: "nope" }).errors.email).toBeTruthy();
    expect(validateApproverForm({ name: "", phone: "+14155550101", email: "" }).errors.name).toBeTruthy();
    const ok = validateApproverForm({ name: " Ann ", phone: "+14155550101", email: "" });
    expect(ok.errors).toEqual({});
    expect(ok.payload).toEqual({ name: "Ann", phone_number: "+14155550101", email: null });
  });
});

describe("request form validation", () => {
  it("bounds lengths and requires a JSON object context", () => {
    expect(validateRequestForm({ policyId: "p", action: "x".repeat(201), summary: "s", context: "" }).errors.action).toBeTruthy();
    expect(validateRequestForm({ policyId: "p", action: "a", summary: "s".repeat(2001), context: "" }).errors.summary).toBeTruthy();
    expect(validateRequestForm({ policyId: "p", action: "a", summary: "s", context: "{bad" }).errors.context).toMatch(/not valid JSON/);
    expect(validateRequestForm({ policyId: "p", action: "a", summary: "s", context: `{"x":"${"y".repeat(17000)}"}` }).errors.context).toMatch(/16 KB/);
    const ok = validateRequestForm({ policyId: "p", action: " a ", summary: "s", context: '{"k": 1}' });
    expect(ok.errors).toEqual({});
    expect(ok.payload).toEqual({ policy_id: "p", action: "a", summary: "s", context: { k: 1 } });
  });
});

describe("no secrets in browser code", () => {
  // Every application source file as raw text (test files excluded), loaded by Vite.
  const sources = import.meta.glob<string>("../**/*.{ts,tsx}", { query: "?raw", import: "default", eager: true });
  // Paths are relative to this file: "./x" is the test directory itself; app code is "../<dir>/x".
  const appFiles = Object.entries(sources).filter(([path]) => path.startsWith("../") && !path.includes("/test/"));

  it("scans the application sources", () => {
    expect(appFiles.length).toBeGreaterThan(10);
  });

  it("never sends API keys, reads secret env vars or stores credentials in the browser", () => {
    for (const [file, text] of appFiles) {
      expect(text, file).not.toMatch(/X-API-Key/i);
      expect(text, file).not.toMatch(/import.meta.env.VITE_/);
      expect(text, file).not.toMatch(/localStorage|sessionStorage|document.cookie/);
      expect(text, file).not.toMatch(/al_[A-Za-z0-9_-]{20,}/);
    }
  });
});

describe("deadline display", () => {
  it("never shows a final decision's deadline as passed", async () => {
    const { deadlineNote } = await import("../pages/RequestDetailPage");
    const ev = (deadline_passed: boolean) => ({ deadline_passed }) as never;
    expect(deadlineNote({ status: "approved", evaluation: ev(true) })).toContain("decided before");
    expect(deadlineNote({ status: "rejected", evaluation: ev(true) })).toContain("decided before");
    expect(deadlineNote({ status: "pending", evaluation: ev(true) })).toBe(" — passed");
    expect(deadlineNote({ status: "expired", evaluation: ev(true) })).toBe(" — passed");
    expect(deadlineNote({ status: "pending", evaluation: ev(false) })).toBe("");
  });
});
