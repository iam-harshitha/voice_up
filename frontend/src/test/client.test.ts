import { describe, expect, it, vi } from "vitest";
import { ApiError, apiRequest, setCsrfToken, setUnauthorizedHandler } from "../api/client";
import { mockApi } from "./harness";

describe("api client", () => {
  it("uses the session cookie (same-origin) and never an API key header", async () => {
    const api = mockApi({ "GET /requests": { status: 200, body: { items: [], total: 0, limit: 25, offset: 0 } } });
    await apiRequest("GET", "/requests");
    const init = vi.mocked(fetch).mock.calls[0]![1]!;
    expect(init.credentials).toBe("same-origin");
    expect(api.calls[0]!.headers.has("X-API-Key")).toBe(false);
    expect(api.calls[0]!.headers.has("X-CSRF-Token")).toBe(false); // not on reads
  });

  it("sends the CSRF token on state-changing requests", async () => {
    setCsrfToken("csrf-abc");
    const api = mockApi({ "POST /approvers": { status: 201, body: { id: "1" } } });
    await apiRequest("POST", "/approvers", { body: { name: "x" } });
    expect(api.calls[0]!.headers.get("X-CSRF-Token")).toBe("csrf-abc");
    setCsrfToken(null);
  });

  it("parses the backend error contract, including validation details", async () => {
    mockApi({
      "POST /policies": {
        status: 422,
        body: {
          error: {
            code: "validation_error",
            message: "The request is invalid.",
            request_id: "req-1",
            details: [{ loc: ["body", "required_approvals"], msg: "Input should be 1 or 2", type: "literal_error" }],
          },
        },
      },
    });
    const error = await apiRequest("POST", "/policies", { body: {} }).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    const apiError = error as ApiError;
    expect(apiError.status).toBe(422);
    expect(apiError.requestId).toBe("req-1");
    expect(apiError.userMessage).toContain("required_approvals: Input should be 1 or 2");
  });

  it.each([
    [403, "insufficient_scope", "permission"],
    [403, "csrf_failed", "could not be verified"],
    [404, "request_not_found", "Not found"],
    [409, "seat_already_decided", "This approver's seat has already been decided"],
    [429, "rate_limited", "Too many requests"],
    [500, "internal_error", "Nothing was confirmed"],
  ])("maps HTTP %i (%s) to a clear message", async (status, code, text) => {
    const message = code === "seat_already_decided" ? "This approver's seat has already been decided by another approver." : "x";
    mockApi({ "GET /thing": { status, body: { error: { code, message } } } });
    const error = (await apiRequest("GET", "/thing").catch((e: unknown) => e)) as ApiError;
    expect(error.code).toBe(code);
    expect(error.userMessage).toContain(text);
  });

  it("notifies on 401 so the app returns to sign-in", async () => {
    const onUnauthorized = vi.fn();
    setUnauthorizedHandler(onUnauthorized);
    mockApi({ "GET /requests": { status: 401, body: { error: { code: "session_invalid", message: "expired" } } } });
    await expect(apiRequest("GET", "/requests")).rejects.toBeInstanceOf(ApiError);
    expect(onUnauthorized).toHaveBeenCalledOnce();
    setUnauthorizedHandler(null);
  });

  it("never turns a network failure into data", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("Failed to fetch"));
    const error = (await apiRequest("GET", "/requests").catch((e: unknown) => e)) as ApiError;
    expect(error.status).toBe(0);
    expect(error.userMessage).toContain("could not be reached");
  });

  it("refuses unreadable responses", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("<html>oops</html>", { status: 200 }));
    const error = (await apiRequest("GET", "/requests").catch((e: unknown) => e)) as ApiError;
    expect(error.code).toBe("malformed_response");
  });

  it("times out instead of hanging", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(
      (_input, init) =>
        new Promise((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
        }),
    );
    const error = (await apiRequest("GET", "/slow", { timeoutMs: 20 }).catch((e: unknown) => e)) as ApiError;
    expect(error.code).toBe("timeout");
  });

  it("does not retry mutations", async () => {
    const api = mockApi({ "POST /requests": { status: 503, body: { error: { code: "unavailable", message: "x" } } } });
    await expect(apiRequest("POST", "/requests", { body: {} })).rejects.toBeInstanceOf(ApiError);
    expect(api.calls).toHaveLength(1);
  });
});
