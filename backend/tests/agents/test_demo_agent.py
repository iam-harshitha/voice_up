"""Demo agent: the simulated deployment runs only after an explicit, matching approval.

The gateway is a scripted fake so every hostile or ambiguous backend answer can be
produced; the parsing, waiting and gate code under test is the real code. The REST and
MCP gateways are exercised against the real backend in tests/integration/test_chunk4_e2e.py.
"""

import asyncio
import uuid
from pathlib import Path

import pytest

from approval_line_agents.client import (
    ApiResponseError,
    BackendUnavailableError,
    MalformedResponseError,
    parse_status,
)
from approval_line_agents.demo import (
    ACTION,
    EXIT_CREATE_FAILED,
    EXIT_EXECUTED,
    EXIT_EXPIRED,
    EXIT_GATE_REFUSED,
    EXIT_MALFORMED,
    EXIT_REJECTED,
    EXIT_TIMEOUT,
    EXIT_UNAVAILABLE,
    SUMMARY,
    Clock,
    McpGateway,
    SimulatedDeployment,
    WaitConfig,
    run_agent,
    status_from_tool_result,
)
from tests.agents.payloads import POLICY_ID, request_payload

RELEASE = "v2.4"
SUMMARY_TEXT = SUMMARY.format(release=RELEASE)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds


class ScriptedGateway:
    """Returns (or raises) the next scripted answer on every status poll."""

    def __init__(self, script, *, request_id=None, create_error=None):
        self.request_id = request_id or uuid.uuid4()
        self.script = list(script)
        self.create_calls = 0
        self.status_calls = 0
        self.create_error = create_error
        self.keys: list[str] = []

    def payload(self, kind, **kw):
        kw.setdefault("summary", SUMMARY_TEXT)
        return request_payload(kind, kw.pop("request_id", self.request_id), **kw)

    async def create(self, policy_id, action, summary, context, idempotency_key):
        self.create_calls += 1
        self.keys.append(idempotency_key)
        if self.create_error:
            raise self.create_error
        assert (policy_id, action, summary) == (POLICY_ID, ACTION, SUMMARY_TEXT)
        return parse_status(self.payload("pending"))

    async def status(self, request_id):
        self.status_calls += 1
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(step, Exception):
            raise step
        if callable(step):
            return step()
        return parse_status(step)


class CountingExecutor:
    def __init__(self, tmp_path: Path):
        self.inner = SimulatedDeployment(tmp_path / "runs")
        self.calls = 0

    def __call__(self, status, release):
        self.calls += 1
        return self.inner(status, release)


def run(gateway, tmp_path, *, timeout=60.0, resume=None, executor=None):
    executor = executor or CountingExecutor(tmp_path)
    lines: list[str] = []
    clock = FakeClock()
    code = asyncio.run(
        run_agent(
            gateway,
            policy_id=POLICY_ID,
            release=RELEASE,
            executor=executor,
            config=WaitConfig(poll_interval=1.0, max_interval=4.0, timeout=timeout),
            report=lines.append,
            resume_request_id=resume,
            clock=Clock(monotonic=clock.monotonic, sleep=clock.sleep),
        )
    )
    return code, executor, lines


def test_1_approved_and_matching_executes_exactly_once(tmp_path):
    gw = ScriptedGateway([])
    gw.script = [gw.payload("pending"), gw.payload("approved")]
    code, executor, lines = run(gw, tmp_path)
    assert code == EXIT_EXECUTED and executor.calls == 1
    record = executor.inner.executions[0]
    text = record.read_text()
    assert str(gw.request_id) in text and '"simulated": true' in text
    assert any("AUTHORIZED" in line for line in lines)
    assert gw.create_calls == 1


@pytest.mark.parametrize(
    ("kind", "code"),
    [
        ("pending", EXIT_TIMEOUT),  # 2
        ("rejected", EXIT_REJECTED),  # 3
        ("expired", EXIT_EXPIRED),  # 4
        ("condition", EXIT_TIMEOUT),  # 5
        ("reapproval", EXIT_TIMEOUT),  # 6
    ],
)
def test_2_to_6_unapproved_states_never_execute(tmp_path, kind, code):
    gw = ScriptedGateway([])
    gw.script = [gw.payload(kind)]
    result, executor, lines = run(gw, tmp_path, timeout=10)
    assert result == code and executor.calls == 0
    assert any("HALTED" in line for line in lines)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p["evaluation"].pop("authorized"),  # 7 missing
        lambda p: p["evaluation"].__setitem__("authorized", "true"),  # 8 wrong type
        lambda p: p["evaluation"].__setitem__("authorized", 1),  # 8 wrong type
    ],
)
def test_7_8_missing_or_mistyped_authorized_never_executes(tmp_path, mutate):
    gw = ScriptedGateway([])

    def bad():
        payload = gw.payload("approved")
        mutate(payload)
        return parse_status(payload)

    gw.script = [bad]
    code, executor, _ = run(gw, tmp_path)
    assert code == EXIT_MALFORMED and executor.calls == 0


def test_9_malformed_json_never_executes(tmp_path):
    gw = ScriptedGateway([MalformedResponseError("malformed_response", "non-JSON body")])
    code, executor, _ = run(gw, tmp_path)
    assert code == EXIT_MALFORMED and executor.calls == 0


def test_10_api_timeouts_never_execute(tmp_path):
    gw = ScriptedGateway([BackendUnavailableError("backend_timeout", "slow")])
    code, executor, _ = run(gw, tmp_path)
    assert code == EXIT_UNAVAILABLE and executor.calls == 0
    assert gw.status_calls == 3  # bounded retries, then stop


def test_10b_transient_outage_then_approval_still_requires_the_approval(tmp_path):
    gw = ScriptedGateway([])
    gw.script = [BackendUnavailableError("backend_timeout", "slow"), gw.payload("approved")]
    code, executor, _ = run(gw, tmp_path)
    assert code == EXIT_EXECUTED and executor.calls == 1


def test_11_polling_timeout_never_executes_and_is_bounded(tmp_path):
    gw = ScriptedGateway([])
    gw.script = [gw.payload("pending")]
    code, executor, _ = run(gw, tmp_path, timeout=30)
    assert code == EXIT_TIMEOUT and executor.calls == 0
    # Backoff 1, 1.5, 2.25, 3.375, then capped at 4: a bounded number of polls.
    assert 5 <= gw.status_calls <= 12


def test_12_request_id_mismatch_never_executes(tmp_path):
    gw = ScriptedGateway([])
    gw.script = [lambda: parse_status(gw.payload("approved", request_id=uuid.uuid4()))]
    code, executor, lines = run(gw, tmp_path)
    assert code == EXIT_GATE_REFUSED and executor.calls == 0
    assert any("request ID" in line for line in lines)


@pytest.mark.parametrize(
    "overrides",
    [
        {"authorized": True, "_status": "pending"},
        {"authorized": False},
        {"state": "awaiting_approvals"},
        {"next_action": "resolve_condition"},
    ],
)
def test_13_contradictory_decision_fields_never_execute(tmp_path, overrides):
    gw = ScriptedGateway([])

    def contradictory():
        status = overrides.pop("_status", None)
        payload = gw.payload("approved", **overrides)
        if status:
            payload["status"] = status
        return parse_status(payload)

    gw.script = [contradictory]
    code, executor, _ = run(gw, tmp_path)
    assert code == EXIT_MALFORMED and executor.calls == 0


def test_14_repeated_approved_polls_and_reruns_never_execute_twice(tmp_path):
    gw = ScriptedGateway([])
    gw.script = [gw.payload("approved")]
    executor = CountingExecutor(tmp_path)
    first, _, _ = run(gw, tmp_path, executor=executor)
    # A second run watching the same request must not deploy again.
    second, _, lines = run(gw, tmp_path, executor=executor, resume=gw.request_id)
    assert first == EXIT_EXECUTED and second == EXIT_GATE_REFUSED
    assert len(executor.inner.executions) == 1
    assert any("already authorized one execution" in line for line in lines)


def test_15_failed_creation_halts_with_a_recoverable_key(tmp_path):
    """The agent asks to create once (the client's own retries reuse the same key); on
    failure it halts, executes nothing and prints the key that recovers the request."""
    gw = ScriptedGateway([], create_error=BackendUnavailableError("backend_timeout", "slow"))
    code, executor, lines = run(gw, tmp_path)
    assert code == EXIT_CREATE_FAILED and executor.calls == 0
    assert gw.create_calls == 1 and gw.status_calls == 0
    key = gw.keys[0]
    assert key.startswith("demo-") and any(f"--idempotency-key {key}" in line for line in lines)


def test_15b_a_supplied_idempotency_key_is_used_for_creation(tmp_path):
    gw = ScriptedGateway([])
    gw.script = [gw.payload("approved")]
    executor = CountingExecutor(tmp_path)
    clock = FakeClock()
    code = asyncio.run(
        run_agent(
            gw,
            policy_id=POLICY_ID,
            release=RELEASE,
            executor=executor,
            config=WaitConfig(poll_interval=1.0, timeout=10),
            report=lambda _line: None,
            clock=Clock(monotonic=clock.monotonic, sleep=clock.sleep),
            idempotency_key="recover-me-123",
        )
    )
    assert code == EXIT_EXECUTED and gw.keys == ["recover-me-123"]


def test_16_satisfied_condition_alone_never_executes(tmp_path):
    gw = ScriptedGateway([])
    # Condition open, then satisfied (re-approval required), and the wait times out.
    gw.script = [gw.payload("condition"), gw.payload("reapproval"), gw.payload("reapproval")]
    code, executor, lines = run(gw, tmp_path, timeout=20)
    assert code == EXIT_TIMEOUT and executor.calls == 0
    assert any("awaiting_reapproval" in line for line in lines)


@pytest.mark.parametrize(("kind", "code"), [("rejected", EXIT_REJECTED), ("expired", EXIT_EXPIRED)])
def test_17_rejection_or_expiry_during_polling_stops_immediately(tmp_path, kind, code):
    gw = ScriptedGateway([])
    gw.script = [
        gw.payload("pending"),
        gw.payload("condition"),
        gw.payload(kind),
        gw.payload("approved"),
    ]
    result, executor, _ = run(gw, tmp_path)
    assert result == code and executor.calls == 0
    assert gw.status_calls == 3  # stopped at the terminal answer, never saw "approved"


def test_gate_refuses_an_approval_for_a_different_action(tmp_path):
    gw = ScriptedGateway([])
    gw.script = [lambda: parse_status(gw.payload("approved", action="drop_database"))]
    code, executor, lines = run(gw, tmp_path)
    assert code == EXIT_GATE_REFUSED and executor.calls == 0
    assert any("different from the intended action" in line or "differs" in line for line in lines)


def test_gate_refuses_an_approval_under_a_different_policy(tmp_path):
    gw = ScriptedGateway([])
    gw.script = [lambda: parse_status(gw.payload("approved", policy_id=uuid.uuid4()))]
    code, executor, _ = run(gw, tmp_path)
    assert code == EXIT_GATE_REFUSED and executor.calls == 0


def test_a_refused_status_check_stops_without_execution(tmp_path):
    gw = ScriptedGateway([ApiResponseError("request_not_found", "gone", status_code=404)])
    code, executor, _ = run(gw, tmp_path)
    assert code == EXIT_UNAVAILABLE and executor.calls == 0


class FakeToolResult:
    def __init__(self, structured=None, error=None):
        self.structured_content = structured
        self.is_error = error is not None
        self.content = [type("T", (), {"text": error})()] if error else []


class FakeSession:
    def __init__(self, result):
        self.result = result

    async def call_tool(self, name, arguments):
        return self.result


def mcp_shape(payload):
    e = payload["evaluation"]
    return {
        "request_id": payload["id"],
        "action": payload["action"],
        "summary": payload["summary"],
        "policy_id": payload["policy_id"],
        "policy_version": 1,
        "status": payload["status"],
        "authorized": e["authorized"],
        "state": e["state"],
        "reason": e["reason"],
        "next_action": e["next_action"],
        "approvals": e["approvals"],
        "rejections": e["rejections"],
        "required_approvals": 2,
        "outstanding_approver_ids": [],
        "condition_approver_ids": e["condition_approver_ids"],
        "reapproval_approver_ids": e["reapproval_approver_ids"],
        "deadline_passed": e["deadline_passed"],
        "expires_at": None,
        "decided_at": payload["decided_at"],
        "created_at": payload["created_at"],
        "guidance": "x",
    }


def test_mcp_tool_results_are_revalidated_by_the_agent():
    rid = uuid.uuid4()
    good = mcp_shape(request_payload("approved", rid))
    assert status_from_tool_result(good).authorized is True
    for broken in (
        {**good, "authorized": "true"},
        {k: v for k, v in good.items() if k != "authorized"},
        {**good, "status": "pending"},
        None,
        "approved",
    ):
        with pytest.raises(MalformedResponseError):
            status_from_tool_result(broken)


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("backend_unavailable (backend_timeout): ... do not act.", BackendUnavailableError),
        ("untrusted_response (contradictory_response): ...", MalformedResponseError),
        ("permission_denied (insufficient_scope): ...", ApiResponseError),
    ],
)
def test_mcp_tool_errors_become_typed_errors(text, error):
    gateway = McpGateway(FakeSession(FakeToolResult(error=text)))
    with pytest.raises(error):
        asyncio.run(gateway.status(uuid.uuid4()))


def test_mcp_gateway_refuses_a_different_request():
    gateway = McpGateway(
        FakeSession(FakeToolResult(mcp_shape(request_payload("approved", uuid.uuid4()))))
    )
    with pytest.raises(MalformedResponseError, match="different request"):
        asyncio.run(gateway.status(uuid.uuid4()))


def test_concurrent_executions_for_one_request_run_at_most_once(tmp_path):
    """The execution record is claimed with an exclusive create: when many workers race
    for the same approved request on one filesystem, exactly one runs."""
    import threading

    status = parse_status(request_payload("approved", uuid.uuid4(), summary=SUMMARY_TEXT))
    deployment = SimulatedDeployment(tmp_path / "runs")
    barrier = threading.Barrier(8)
    outcomes: list[str] = []
    lock = threading.Lock()

    def attempt():
        barrier.wait()
        try:
            deployment(status, RELEASE)
            result = "ran"
        except FileExistsError:
            result = "refused"
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert sorted(outcomes) == ["ran"] + ["refused"] * 7
    assert len(list((tmp_path / "runs").iterdir())) == 1


# --- F1: deadline semantics at the execution gate -----------------------------------------
#
# The backend refuses votes at or after expires_at, so a genuine approval is decided
# strictly before its deadline. evaluation.deadline_passed only reports that the clock is
# now past expires_at, which is also true for an approval observed late.

DEADLINE = "2026-10-24T11:00:00Z"


def with_deadline(payload, *, expires_at=DEADLINE, decided_at=None, deadline_passed=True):
    payload["expires_at"] = expires_at
    payload["evaluation"]["expires_at"] = expires_at
    payload["evaluation"]["deadline_passed"] = deadline_passed
    if decided_at is not None:
        payload["decided_at"] = decided_at
    return payload


def test_approval_observed_after_the_deadline_still_executes(tmp_path):
    """Approved at 10:05, deadline 11:00, agent polls after 11:00 (slow poll)."""
    gw = ScriptedGateway([])
    gw.script = [gw.payload("pending"), with_deadline(gw.payload("approved"))]
    code, executor, lines = run(gw, tmp_path)
    assert code == EXIT_EXECUTED and executor.calls == 1, lines


def test_agent_restart_after_the_deadline_executes_a_prior_approval_once(tmp_path):
    gw = ScriptedGateway([])
    gw.script = [with_deadline(gw.payload("approved"))]
    executor = CountingExecutor(tmp_path)
    first, _, _ = run(gw, tmp_path, executor=executor, resume=gw.request_id)
    second, _, _ = run(gw, tmp_path, executor=executor, resume=gw.request_id)
    assert (first, second) == (EXIT_EXECUTED, EXIT_GATE_REFUSED)
    assert len(executor.inner.executions) == 1


@pytest.mark.parametrize(
    "decided_at",
    ["2026-10-24T11:00:00Z", "2026-10-24T11:00:01Z"],  # at or after the deadline
)
def test_an_approval_decided_at_or_after_its_deadline_is_refused(tmp_path, decided_at):
    gw = ScriptedGateway([])
    gw.script = [lambda: parse_status(with_deadline(gw.payload("approved"), decided_at=decided_at))]
    code, executor, lines = run(gw, tmp_path)
    assert code == EXIT_GATE_REFUSED and executor.calls == 0
    assert any("not decided before the request's deadline" in line for line in lines)


def test_approval_without_a_deadline_is_unaffected(tmp_path):
    gw = ScriptedGateway([])
    gw.script = [gw.payload("approved")]  # expires_at None, deadline_passed False
    code, executor, _ = run(gw, tmp_path)
    assert code == EXIT_EXECUTED and executor.calls == 1


@pytest.mark.parametrize(
    "kind",
    ["pending", "condition", "reapproval"],
)
def test_undecided_requests_past_their_deadline_never_execute(tmp_path, kind):
    """Pending, condition-blocked or awaiting re-approval when the deadline passes: the
    backend reports deadline_passed (status still pending until the worker expires it)."""
    gw = ScriptedGateway([])
    gw.script = [with_deadline(gw.payload(kind))]
    code, executor, _ = run(gw, tmp_path)
    assert code == EXIT_EXPIRED and executor.calls == 0


@pytest.mark.parametrize("kind", ["expired", "rejected"])
def test_final_unapproved_requests_past_their_deadline_never_execute(tmp_path, kind):
    gw = ScriptedGateway([])
    gw.script = [with_deadline(gw.payload(kind))]
    code, executor, _ = run(gw, tmp_path)
    assert code in (EXIT_EXPIRED, EXIT_REJECTED) and executor.calls == 0
