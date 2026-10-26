# Approval Line dashboard

An operator dashboard for Approval Line, built with React 18, TypeScript (strict), Vite 5, Tailwind CSS 3 and React Router 6. Tests use Vitest and Testing Library.

It renders what the backend returns and never decides anything itself. A request is shown as authorized only when the API reports `status: "approved"` **and** `evaluation.authorized: true`. Design, security model and API contracts are in [`backend/docs/dashboard-mcp.md`](../backend/docs/dashboard-mcp.md).

## Run

The API must already be running on `127.0.0.1:8000` (see `backend/README.md`). You also need a dashboard user, created with `python -m app.cli create-dashboard-user`.

```powershell
cd frontend
npm ci            # installs exactly what package-lock.json pins
npm run dev       # http://127.0.0.1:5173, proxies /api to the backend
```

To point the dev proxy at another backend, set `APPROVAL_LINE_BACKEND_URL` (a server-side Vite setting; it is never sent to the browser).

| Command | Purpose |
|---|---|
| `npm run typecheck` | `tsc` with strict settings |
| `npm test` | Unit and component tests (jsdom, mocked `fetch` with backend-shaped responses) |
| `npm run build` | Type check, then a production build into `dist/` (no source maps) |
| `npm run check:bundle` | Fails if `dist/` contains API keys, `X-API-Key`, server setting names, database URLs, browser storage or source maps |

**Deploying:** serve `dist/` and the API on **one origin**, with a reverse proxy routing `/api/` to FastAPI, so the session cookie (`HttpOnly`, `SameSite=Strict`, `Path=/api/v1`) is first-party. Cross-origin hosting is not supported.

## Security

- **No API keys in the browser.** Authentication is a server-side session in an HttpOnly cookie. JavaScript never sees the session token, and nothing is written to `localStorage` or `sessionStorage`.
- **CSRF.** Every POST and PATCH sends the CSRF token returned at sign-in, held in memory only.
- **Scope-aware UI.** Controls the account's scopes do not allow are hidden or marked unavailable. The backend enforces scopes regardless.
- **No voting.** The dashboard cannot vote. Votes come from verified voice calls or a trusted integration key.
- **Contact details are masked.** Approver phone numbers and emails appear only on the Approvers page, masked until revealed.
- **No `VITE_*` secrets.** There are no `VITE_*` variables at all.

## Structure

```
src/
  api/        client.ts (fetch, timeouts, CSRF, typed errors), endpoints.ts, types.ts
  auth/       AuthContext.tsx (session bootstrap, sign-in/out, scope checks)
  hooks/      usePolling.ts (bounded, non-overlapping refresh; pauses in hidden tabs)
  components/ ui.tsx (badges, banners, states, dialog, toasts), Layout.tsx, Timeline.tsx
  pages/      Overview, Requests, RequestDetail, NewRequest, Policies, Approvers, Login
  lib/        format.ts (labels for backend codes; unknown codes shown as-is)
  test/       harness + tests
```

## Data freshness

- **Request detail:** refreshes every 4 s while the request is pending, then stops once it is final.
- **Overview and request list:** refresh every 10 s.
- **Refresh rules:** requests never overlap, refreshing pauses while the tab is hidden, and in-flight requests are cancelled on navigation.
- **Failures:** the last good data stays visible, with an "out of date" notice.

## Limitations

- **No text search.** The API filters by status only.
- **Policies and approvers load up to 100 each.** The pickers use the first page.
- **Polling, not push updates.**
