// Central API client. Authentication is the HttpOnly session cookie set by the backend;
// this code never sees or stores an API key. State-changing requests carry the CSRF
// token returned at sign-in. Mutations are never retried automatically.

import type { ApiErrorBody } from "./types";

export const API_BASE = "/api/v1";
const DEFAULT_TIMEOUT_MS = 15_000;

let csrfToken: string | null = null;
let onUnauthorized: (() => void) | null = null;

export function setCsrfToken(token: string | null): void {
  csrfToken = token;
}

export function setUnauthorizedHandler(handler: (() => void) | null): void {
  onUnauthorized = handler;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId: string | null;
  readonly details: { field: string; message: string }[];

  constructor(
    status: number,
    code: string,
    message: string,
    requestId: string | null = null,
    details: { field: string; message: string }[] = [],
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.requestId = requestId;
    this.details = details;
  }

  /** A message suitable for showing to the user (never raw internals). */
  get userMessage(): string {
    switch (this.status) {
      case 0:
        return this.code === "timeout"
          ? "The server did not respond in time. Nothing was confirmed; try again."
          : "The server could not be reached. Check that the API is running.";
      case 401:
        return "Your session has ended. Please sign in again.";
      case 403:
        return this.code === "csrf_failed"
          ? "The request could not be verified. Reload the page and try again."
          : `You do not have permission for this action. ${this.message}`;
      case 404:
        return "Not found (it may belong to another organization or no longer exist).";
      case 422:
        return this.details.length
          ? `Invalid input: ${this.details.map((d) => `${d.field}: ${d.message}`).join("; ")}`
          : "The request is invalid.";
      case 429:
        return "Too many requests. Wait a moment and try again.";
      default:
        if (this.status >= 500) {
          return "The server had a problem. Nothing was confirmed; try again later.";
        }
        return this.message;
    }
  }
}

type Method = "GET" | "POST" | "PATCH";

interface RequestOptions {
  body?: unknown;
  signal?: AbortSignal;
  timeoutMs?: number;
  query?: Record<string, string | number | boolean | null | undefined>;
}

function buildUrl(path: string, query?: RequestOptions["query"]): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value !== undefined && value !== null && value !== "") params.set(key, String(value));
  }
  const qs = params.toString();
  return `${API_BASE}${path}${qs ? `?${qs}` : ""}`;
}

function parseError(status: number, body: unknown): ApiError {
  const error = (body as Partial<ApiErrorBody> | null)?.error;
  if (error && typeof error.code === "string" && typeof error.message === "string") {
    const details = (error.details ?? []).map((d) => ({
      field: d.loc.filter((part) => part !== "body").join("."),
      message: d.msg,
    }));
    return new ApiError(status, error.code, error.message, error.request_id ?? null, details);
  }
  return new ApiError(status, `http_${status}`, `HTTP ${status}`);
}

export async function apiRequest<T>(method: Method, path: string, options: RequestOptions = {}): Promise<T> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort("timeout"), options.timeoutMs ?? DEFAULT_TIMEOUT_MS);
  const onAbort = () => controller.abort("cancelled");
  options.signal?.addEventListener("abort", onAbort, { once: true });

  const headers: Record<string, string> = { Accept: "application/json" };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET" && csrfToken) headers["X-CSRF-Token"] = csrfToken;

  let response: Response;
  try {
    response = await fetch(buildUrl(path, options.query), {
      method,
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      credentials: "same-origin",
      signal: controller.signal,
      cache: "no-store",
    });
  } catch (cause) {
    if (controller.signal.aborted && controller.signal.reason === "cancelled") {
      throw new DOMException("cancelled", "AbortError");
    }
    const timedOut = controller.signal.aborted && controller.signal.reason === "timeout";
    throw new ApiError(0, timedOut ? "timeout" : "network_error", String(cause));
  } finally {
    clearTimeout(timeout);
    options.signal?.removeEventListener("abort", onAbort);
  }

  if (response.status === 204) return undefined as T;
  let body: unknown = null;
  const text = await response.text();
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      throw new ApiError(response.status, "malformed_response", "The server returned an unreadable response.");
    }
  }
  if (!response.ok) {
    const error = parseError(response.status, body);
    if (response.status === 401 && onUnauthorized && !path.startsWith("/auth/")) onUnauthorized();
    throw error;
  }
  return body as T;
}

export function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}
