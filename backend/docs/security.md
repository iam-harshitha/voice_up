# Security model and known limitations

## Core invariant

**An agent may act only when the backend reports that exact request as `status: "approved"` with `evaluation.authorized: true`.** Five properties back this:

1. **Single decision point.** `app/domain/approval_rules.py` is the only code that computes a decision. Callers, voice providers and agents submit votes or read state; none of them can set a decision.
2. **No path for agents to decide.**
   - Agent keys hold `requests:create, requests:read`.
   - `votes:write` is never in agent scopes, and it can never be granted to dashboard users.
   - The MCP server has no voting, resolving, status-changing or generic-HTTP tools.
3. **Fail closed.**
   - **What counts as authorized:** the REST client, the MCP server and the demo agent treat missing, mistyped, `null` or contradictory authorization data as an error, never as approval.
   - **Errors:** timeouts and 5xx mean "unknown, do not act".
   - **The execution gate also checks:** request ID, action, summary, policy, status, `authorized is True`, `next_action == "none"`, that `decided_at` is set, and, when there is a deadline, that the approval was decided before it. Observing an approval after the deadline is fine; undecided requests past their deadline never execute.
4. **Conditions never approve.** A satisfied condition still needs a fresh, unconditional approval from the approver who stated it.
5. **Calls are not votes.** A call becomes a vote only on a verified execution with an explicit, confident decision from a confirmed identity. A completed call without a decision changes nothing (tested).

## Controls

| Area | Control | Evidence (tests) |
|---|---|---|
| API keys | 256-bit random, stored as SHA-256; scoped; revocable | `test_authentication.py` |
| Tenant isolation | Organization comes from the credential; composite foreign keys; 404 across organizations | many integration tests, `test_dashboard_auth.py` |
| Dashboard sign-in | scrypt hashes; HttpOnly, SameSite=Strict cookie (Secure in production); CSRF token on every non-GET; account lockout; absolute expiry; revocation on logout or disable | `test_dashboard_auth.py`, `test_dashboard_security.py` |
| Browser | No API key in browser code; nothing in local or session storage; bundle secret scan; no source maps | `frontend` tests, `npm run check:bundle` |
| Webhooks | Constant-time token check; refused when no token is configured; deduplicated; decisions re-fetched from the provider with our key | `test_voice_webhooks.py`, `test_chunk5_reliability.py` |
| Production configuration | Refuses mock voice, insecure cookies, wildcard or http CORS, missing Bolna secrets; hides docs | `test_dashboard_security.py` |
| Request creation | Optional `Idempotency-Key`: organization-scoped, stored as a hash, payload-fingerprinted, race-safe | `test_request_idempotency.py` |
| Audit | Insert-only service; database trigger blocks UPDATE and DELETE; no secrets or contact details in event data | `test_audit_timeline.py` |
| Simulated execution | Exclusive-create record per request, so at most one execution per request **per record directory** (`--record-dir`, default `.demo_runs` under the working directory) | `test_demo_agent.py` |

## Known limitations (be explicit about these)

- **Live Bolna is not verified.** No real outbound call and verified callback has been completed. Everything voice-related is verified with the mock provider only.
- **Voice identity is weak.** "Identity confirmed" is the approver's own spoken confirmation, as extracted by the provider. There is no spoken PIN or voice biometrics. Treat voice approvals as appropriate for moderate-risk actions until stronger verification exists.
- **The audit log is not tamper-proof.** The application never updates or deletes audit rows, and triggers block it, but the database owner role (the runtime role, by default) can disable triggers. There is no hash chain or external anchoring. Recommended hardening is in `deployment.md`; it has not been applied.
- **Idempotency covers creation only.** Keys are kept forever, inside the request row. A request created **without** a key cannot be safely retried; every client in this repository sends a key.
- **The execution guarantee is narrow.** The demo agent's "at most once" is enforced by an exclusive file create in its record directory (`--record-dir`, default `.demo_runs` under the working directory).
  - **Scope:** it holds only per record directory. Starting the agent from a different working directory, with a different `--record-dir`, or on another machine uses a separate record, and the same approved request could execute again.
  - **Not exactly-once:** it is not a distributed exactly-once guarantee.
  - **Crashes:** a crash after the record file is created but before it is written leaves the request marked as executed, which fails closed.
- **Sessions:** account lockout but no per-IP rate limit; no MFA or SSO; absolute expiry with no idle timeout; no user-facing session list.
- **Webhook authentication** is a static shared token plus provider re-fetch. There is no signature verification (Bolna does not offer one, per `voice-bolna.md`).
- **Not deployed:** there has been no public deployment, penetration test or certification.
