# Demo and pitch guide

Everything here describes what the code does and what has been verified. Approval Line has no customers, revenue, certifications or production deployment, and its live Bolna calling has been verified only locally, behind a tunnel, with single-approver policies (see `voice-bolna.md`, Live verification). Don't imply otherwise.

## The problem, in plain language

AI agents are starting to take real actions: deploying code, changing infrastructure, moving money. Most teams either block those actions entirely or trust a prompt that says "ask a human first". Neither works:
- Blocking wastes the agent.
- A prompt is not a control: nothing stops the agent from acting anyway, and nobody can prove later who approved what.

## What Approval Line is

A human-authorization service for AI agents:
1. The agent asks for permission through an API or an MCP tool.
2. The organization's policy decides who must approve: unanimous or quorum, vetoes, backups, deadlines, conditions.
3. Approvers are **phoned** and answer by voice.
4. The agent gets one authoritative answer.

**The invariant:** the agent acts only when the backend reports that exact request as `approved` with `authorized: true`. Pending, rejected, expired, condition-blocked, malformed and unreachable all mean **do not act**.

## Architecture in one breath

- **Interfaces:** a FastAPI backend with PostgreSQL. Agents call it with a REST API key or through the MCP stdio server; humans use the React dashboard with a session cookie and CSRF.
- **One decision point:** a pure policy engine is the only code that decides.
- **Voice:** calls go through a durable PostgreSQL job queue and worker, with provider webhooks re-verified against the provider.
- **Audit:** every transition writes an append-only audit event in the same transaction.

## 60–90 second demo script

Pre-start with `.\scripts\start-demo.ps1` and seed with `demo-seed`. Open the dashboard. The voice banner reads **SIMULATED**: say so.

1. **(0:00)** "This agent wants to deploy v2.4 to production. The release includes a database migration, so it needs two humans." Run `python -m approval_line_agents.demo --policy-id <id>`. Point at the request ID it prints and its status: **pending, not authorized**.
2. **(0:15)** Show the same request in the dashboard. Click **Schedule calls**. "These calls are simulated in this demo; the voice path is identical with Bolna."
3. **(0:25)** Alice approves by phone (`voice-simulate ... approve`). The dashboard shows **1 of 2, not authorized**, and the agent is still waiting.
4. **(0:35)** Bob says "only after the backup is verified" (`conditional_approve`). The dashboard shows **Waiting for condition**. "A condition is not a yes."
5. **(0:45)** Resolve the condition in the dashboard. It now shows **Waiting for re-approval**. "Even a satisfied condition needs Bob to say yes again, unconditionally."
6. **(0:55)** Bob re-approves. The dashboard flips to **Authorized**, and the agent prints `AUTHORIZED` and writes its simulated deployment record with the request ID.
7. **(1:10)** Open the audit timeline. "Every call, vote, condition and decision is recorded in order. Had Bob said no, the agent would have stopped. We've tested that, along with expiry, timeouts, malformed responses and lost network replies."

## What the demo proves

- The agent cannot act on pending, conditional, rejected or expired requests. The live runs exited 12, 12, 10 and 11, and nothing executed.
- Authorization comes only from the backend's policy evaluation. The dashboard and the MCP tool show the same persisted request.
- A conditional approval never authorizes by itself.
- Duplicate deliveries and retried creations don't duplicate anything: idempotent creation and deduplicated webhooks are tested and were exercised live.

## What is simulated

- **Phone calls:** placed by the mock provider and answered with `voice-simulate`. The answers still pass through the real webhook endpoint, queue, worker and engine.
- **The deployment:** a JSON file. Nothing is deployed.

## Fallback plan

- **No network or no Bolna:** stay in mock mode. It runs entirely on localhost.
- **A live call fails:** show that the failed call is recorded as `failed` / `no_decision`, and that the agent stays blocked. That is the point of the product.
- **The dashboard misbehaves:** show the agent output and `GET /api/v1/requests/{id}`, or run the mock-backed end-to-end tests (`pytest tests/integration/test_chunk4_e2e.py -q`, about 1 minute).

## Likely judge questions

**"What stops the agent from just skipping the approval?"**
Approval Line can't control code it doesn't run. It makes skipping detectable and keeps credentials scoped:
- The agent's key can only create and read requests. It can't vote, resolve conditions or change status, and the MCP server exposes no such tools.
- Every decision is in the audit log.

The demo agent shows the integration pattern: a hard execution gate that checks request ID, action, status and `authorized is True`. In production, the protected system (the CD pipeline or payment service) should check the approval itself before it executes.

**"How do you know it was really Alice on the phone?"**
Today the voice agent asks the person to confirm their identity, and only an explicit, confident, identity-confirmed decision on a provider-verified call counts. That's moderate assurance, not strong authentication. A spoken PIN or out-of-band confirmation is the next step, and we don't claim more. Live Bolna calling also isn't verified yet; everything shown is the mock provider.

**"What happens if the network drops or the agent retries?"**
Every component fails closed:
- **Lost reply during creation:** the agent retries with the same `Idempotency-Key` and gets the same request back. This is tested with a deliberately dropped response, and was shown live with two runs on one key producing one request.
- **Status unclear:** timeouts and malformed answers stop the agent.
- **Duplicate webhooks:** they produce a single vote.
- **Worker crash:** jobs are leased and recovered; an ambiguous call is never automatically re-dialled.
