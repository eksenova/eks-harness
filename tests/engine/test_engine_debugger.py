from __future__ import annotations

import re
import threading
import time
from collections.abc import Iterator, Mapping
from typing import Any

import pytest
from pydantic import JsonValue

from eks_harness.engine.debugger import (
    CONDITION_MAX_DEPTH,
    CONDITION_MAX_LENGTH,
    BreakpointSpec,
    Condition,
    ConditionError,
    ControlTicket,
    Debugger,
    NodeProbe,
    PauseDecision,
    parse_breakpoint,
)
from eks_harness.engine.model import (
    ControlRequest,
    ControlResponse,
    NodeError,
    PauseInfo,
    RunStatus,
)
from eks_harness.ir.registry import Registry, default_registry

N12 = "n0000000012"
N13 = "n0000000013"
OFF = "debugging is off for this run; start it with --debug"


@pytest.fixture(autouse=True)
def registry() -> Iterator[Registry]:
    chosen = default_registry()
    state = chosen.snapshot()
    try:
        yield chosen
    finally:
        chosen.restore(state)


class Journal:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, JsonValue]]] = []

    def __call__(self, event: str, detail: Mapping[str, JsonValue]) -> None:
        self.events.append((event, dict(detail)))

    def of(self, event: str) -> list[dict[str, JsonValue]]:
        return [detail for name, detail in self.events if name == event]


def probe(node: str = N12, op: str = "api.post", *, label: str | None = None, attempt: int = 1,
          args: JsonValue = None, session: str | None = "main", error: NodeError | None = None) -> NodeProbe:
    return NodeProbe(node_id=node, instance="", attempt=attempt, op=op, label=label, src="flows/a.py:3",
                     args=args, session=session, error=error)


def make(*, enabled: bool = True, breakpoints: tuple[str, ...] = (), break_on_error: bool = False,
         status: RunStatus = "running") -> tuple[Debugger, Journal]:
    journal = Journal()
    debugger = Debugger(enabled=enabled, status=lambda: status, breakpoints=breakpoints,
                        break_on_error=break_on_error, journal=journal)
    return debugger, journal


def pause_info(reason: str = "breakpoint") -> PauseInfo:
    return PauseInfo.model_validate({"reason": reason, "node": N12, "at": time.time()})


def test_parse_breakpoint_forms() -> None:
    assert parse_breakpoint(N12) == BreakpointSpec(text=N12, node=N12)
    assert parse_breakpoint(f"node:{N12}") == BreakpointSpec(text=f"node:{N12}", node=N12)
    assert parse_breakpoint("op:ui.*") == BreakpointSpec(text="op:ui.*", op="ui.*")
    assert parse_breakpoint("label:before submit") == BreakpointSpec(text="label:before submit", label="before submit")
    text = "op:api.*,when:attempt > 1 and contains(args.path, ',')"
    assert parse_breakpoint(text) == BreakpointSpec(text=text, op="api.*",
                                                    condition="attempt > 1 and contains(args.path, ',')")
    combined = parse_breakpoint(f" node:{N12} , label:x ,when: attempt == 2")
    assert (combined.node, combined.label, combined.condition) == (N12, "x", "attempt == 2")


@pytest.mark.parametrize(
    "text",
    ["", "   ", "foo:bar", "node:bad id", "when:__import__('os')", "op:", "op:a,,label:b", "op:a,", "bogus",
     "op:a,op:b", "when:attempt >", f"label:x,{N12},node:{N13}", "when:matches(op, '(')",
     r"when:matches(op, '(a)\\1')", "when:matches(op, label)"],
)
def test_parse_breakpoint_errors(text: str) -> None:
    with pytest.raises(ValueError) as raised:
        parse_breakpoint(text)
    assert repr(text) in str(raised.value)


def evaluate(text: str, scope: Mapping[str, JsonValue] | None = None) -> bool:
    return Condition(text).evaluate(scope or {})


SCOPE: dict[str, JsonValue] = {
    "node": N12, "op": "api.post", "label": "Submit", "src": "flows/a.py:3", "attempt": 2, "instance": "",
    "args": {"path": "/api/a,b", "items": [1, 2, 3], "nested": {"k": "V"}},
    "values": {N13: {"id": 7}}, "error": None,
}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("True", True),
        ("None", False),
        ("attempt > 1", True),
        ("1 < attempt <= 2", True),
        ("1 < attempt < 2", False),
        ("op == 'api.post' and node != 'x'", True),
        ("op != 'api.post' or attempt == 2", True),
        ("not error", True),
        ("args.path", True),
        ("args.missing", False),
        ("args.missing.deeper == None", True),
        ("args['nested']['k'] == 'V'", True),
        ("args.items[0] == 1 and args.items[-1] == 3", True),
        ("args.items[7] == None", True),
        ("values['n0000000013'].id == 7", True),
        ("2 in args.items", True),
        ("5 not in args.items", True),
        ("'path' in args", True),
        ("',' in args.path", True),
        ("-attempt < 0", True),
        ("attempt * 2 + 1 - 1 == 4", True),
        ("attempt / 2 == 1 and attempt % 2 == 0", True),
        ("'a' + 'b' == 'ab'", True),
        ("[1, 2] + [3] == args.items", True),
        ("(1, 2) == [1, 2]", True),
        ("len(args.items) == 3 and len(args.path) == 8", True),
        ("contains(args.path, ',')", True),
        ("contains(args.items, 4)", False),
        ("contains(args.missing, 'x')", False),
        ("startswith(args.path, '/api') and endswith(src, ':3')", True),
        ("matches(args.path, '^/api/[a-z],b$')", True),
        ("lower(label) == 'submit' and upper(label) == 'SUBMIT'", True),
        ("lower(args.missing) == None", True),
        ("instance == ''", True),
    ],
)
def test_condition_accepts(text: str, expected: bool) -> None:
    assert evaluate(text, SCOPE) is expected


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("__import__('os')", "calling '__import__'"),
        ("print(1)", "calling 'print'"),
        ("args.path.upper()", "calling"),
        ("len(args, x=1)", "keyword arguments"),
        ("len(*args)", "starred"),
        ("(lambda: 1)()", "calling"),
        ("lambda: 1", "lambda"),
        ("[x for x in args]", "comprehension"),
        ("{x for x in args}", "comprehension"),
        ("f'{op}'", "f-string"),
        ("[*args.items]", "starred"),
        ("foo == 1", "unknown name 'foo'"),
        ("len", "unknown name 'len'"),
        ("args[op]", "constant"),
        ("args.items[0:1]", "constant"),
        ("1 if op else 2", "conditional expression"),
        ("{'a': 1}", "dict display"),
        ("op is None", "comparison Is"),
        ("2 ** 3", "operator Pow"),
        ("~attempt", "unary operator Invert"),
        ("(x := 1)", "assignment expression"),
        ("b'x' == op", "constant"),
        ("len(1, 2)", "takes 1 argument"),
        ("op ==", "not a valid expression"),
        ("", "empty"),
    ],
)
def test_condition_refuses(text: str, fragment: str) -> None:
    with pytest.raises(ConditionError) as raised:
        Condition(text)
    assert fragment in str(raised.value)


def test_condition_limits() -> None:
    with pytest.raises(ConditionError, match="longer than"):
        Condition("attempt == 1 and " * 80 + "True")
    assert len("attempt == 1 and " * 80) > CONDITION_MAX_LENGTH
    with pytest.raises(ConditionError, match="nests deeper"):
        Condition("not " * (CONDITION_MAX_DEPTH + 1) + "True")
    assert evaluate("not " * (CONDITION_MAX_DEPTH - 1) + "True") is False


@pytest.mark.parametrize(
    "text",
    ["attempt < 'x'", "op - 1", "args.path.x == 1", "attempt / 0 > 1", "len(attempt) == 1", "1 in op",
     "-op == 1", "startswith(args.path, 1)", "args.items['k'] == 1", "error < 1"],
)
def test_condition_type_errors(text: str) -> None:
    condition = Condition(text)
    with pytest.raises(ConditionError):
        condition.evaluate(SCOPE)


def test_probe_scope() -> None:
    failure = NodeError(code="http_status", message="500", detail={"status": 500})
    scope = probe(label="Submit", args={"path": "/x"}, error=failure).scope({N13: 1})
    assert scope == {
        "node": N12, "op": "api.post", "label": "Submit", "src": "flows/a.py:3", "attempt": 1, "instance": "",
        "args": {"path": "/x"}, "values": {N13: 1},
        "error": {"code": "http_status", "message": "500", "detail": {"status": 500}, "retryable": False,
                  "soft": False},
    }
    assert evaluate("error.code == 'http_status' and error.detail.status >= 500", scope) is True


def test_debugging_off() -> None:
    debugger, journal = make(enabled=False, breakpoints=(N12, "op:*"), break_on_error=True)
    assert debugger.state == "off"
    assert [b.id for b in debugger.breakpoints()] == ["b1", "b2"]
    assert len(journal.of("break.added")) == 2
    assert debugger.before_node(probe(), {}) is None
    assert debugger.at_source(probe(op="debug.break"), "x", True) is None
    assert debugger.at_error(probe()) is None
    for action in ("pause", "continue", "step", "retry", "skip", "edit", "do"):
        response = debugger.submit(ControlRequest(action=action), timeout=0.1)
        assert response.ok is False
        assert response.message == OFF
        assert response.debugger == "off"
    status = debugger.submit(ControlRequest(action="status"))
    assert status.ok is True
    assert status.debugger == "off"
    assert status.status == "running"
    assert [b.hits for b in status.breakpoints] == [0, 0]
    listed = debugger.submit(ControlRequest(action="break_list"))
    assert listed.ok is True
    assert len(listed.breakpoints) == 2


def test_breakpoint_matching_and_hits() -> None:
    debugger, journal = make(breakpoints=(N12,))
    assert debugger.state == "running"
    added = journal.of("break.added")
    assert added == [{"breakpoint": {"id": "b1", "text": N12, "node": N12, "op": None, "label": None,
                                     "condition": None, "enabled": True, "hits": 0}}]
    decision = debugger.before_node(probe(), {})
    assert decision == PauseDecision("breakpoint", breakpoint="b1")
    assert debugger.before_node(probe(N13), {}) is None
    assert debugger.breakpoints()[0].hits == 1
    op_glob = debugger.add("op:ui.*")
    assert op_glob.id == "b2"
    assert debugger.before_node(probe(N13, "ui.tap"), {}) == PauseDecision("breakpoint", breakpoint="b2")
    assert debugger.before_node(probe(N13, "api.get"), {}) is None
    labelled = debugger.add("label:Submit")
    assert debugger.before_node(probe(N13, "api.get", label="Submit"), {}) == PauseDecision(
        "breakpoint", breakpoint=labelled.id, label="Submit")
    assert debugger.before_node(probe(N13, "api.get", label="Other"), {}) is None
    assert debugger.before_node(probe(N13, "debug.break", args={"label": "before submit"}), {}) is None
    source = debugger.add("label:before submit")
    decision = debugger.at_source(probe(N13, "debug.break"), "before submit", False)
    assert decision == PauseDecision("breakpoint", breakpoint=source.id, label="before submit")
    assert debugger.at_source(probe(N13, "debug.break"), "elsewhere", True) == PauseDecision(
        "source", label="elsewhere")
    assert debugger.at_source(probe(N13, "debug.break"), "elsewhere", False) is None
    conditional = debugger.add("op:api.*,when:attempt > 1 and values['n0000000013'].id == 7")
    assert debugger.before_node(probe(N13, "api.put", attempt=1), {N13: {"id": 7}}) is None
    assert debugger.before_node(probe(N13, "api.put", attempt=2), {N13: {"id": 8}}) is None
    assert debugger.before_node(probe(N13, "api.put", attempt=2), {N13: {"id": 7}}) == PauseDecision(
        "breakpoint", breakpoint=conditional.id)
    hits = {b.id: b.hits for b in debugger.breakpoints()}
    assert hits == {"b1": 1, "b2": 1, "b3": 1, "b4": 1, "b5": 1}
    assert debugger.remove("b2") is True
    assert debugger.remove("b2") is False
    assert journal.of("break.removed") == [{"id": "b2"}]
    assert debugger.before_node(probe(N13, "ui.tap"), {}) is None
    broken = debugger.add("op:ui.*,when:attempt < 'x'")
    assert debugger.before_node(probe(N13, "ui.tap"), {}) is None
    errors = journal.of("break.error")
    assert len(errors) == 1
    assert errors[0]["id"] == broken.id
    message = errors[0]["message"]
    assert isinstance(message, str)
    assert "compare" in message
    assert next(b for b in debugger.breakpoints() if b.id == broken.id).hits == 0
    assert [name for name, _detail in journal.events].count("break.added") == 6


def test_journal_callback_runs_outside_the_lock() -> None:
    seen: list[str] = []
    debugger_box: list[Debugger] = []

    def journal(event: str, detail: Mapping[str, JsonValue]) -> None:
        if debugger_box:
            outcome: list[str] = []
            worker = threading.Thread(target=lambda: outcome.append(debugger_box[0].state))
            worker.start()
            worker.join(timeout=2)
            seen.append(f"{event}:{outcome[0] if outcome else 'blocked'}")

    debugger = Debugger(enabled=True, status=lambda: "running", journal=journal)
    debugger_box.append(debugger)
    created = debugger.add("op:x,when:attempt < 'y'")
    debugger.before_node(probe(op="x"), {})
    debugger.remove(created.id)
    assert seen == ["break.added:running", "break.error:running", "break.removed:running"]


def test_pause_sources() -> None:
    debugger, _journal = make()
    assert debugger.before_node(probe(), {}) is None
    response = debugger.submit(ControlRequest(action="pause"))
    assert response.ok is True
    assert debugger.before_node(probe(), {}) == PauseDecision("pause")
    assert debugger.before_node(probe(), {}) is None
    debugger.enter_pause(pause_info("pause"))
    assert debugger.state == "paused"
    assert debugger.pause_info is not None
    assert debugger.submit(ControlRequest(action="pause")).message == "the run is already paused"
    debugger.leave_pause("step")
    assert debugger.state == "stepping"
    assert debugger.pause_info is None
    assert debugger.before_node(probe(N13), {}) == PauseDecision("step")
    debugger.enter_pause(pause_info("step"))
    debugger.leave_pause("continue")
    assert debugger.state == "running"
    assert debugger.before_node(probe(N13), {}) is None
    assert debugger.at_error(probe(session="main")) is None
    debugger.set_pause_on_error("main", True)
    assert debugger.at_error(probe(session="main")) == PauseDecision("error")
    assert debugger.at_error(probe(session="take0")) is None
    debugger.set_pause_on_error("main", False)
    assert debugger.at_error(probe(session="main")) is None
    on_error, _ = make(break_on_error=True)
    assert on_error.at_error(probe(session=None)) == PauseDecision("error")


def scheduler_answers(debugger: Debugger, cancel: threading.Event, answered: list[ControlTicket],
                      result: Any = None) -> threading.Thread:
    def serve() -> None:
        ticket = debugger.next_ticket(cancel, timeout=5)
        if ticket is None:
            return
        answered.append(ticket)
        if ticket.request.action == "continue":
            debugger.leave_pause("continue")
        debugger.answer(ticket, ControlResponse(ok=True, status="running", debugger="running",
                                                message=f"{ticket.request.action} done", result=result))

    thread = threading.Thread(target=serve)
    thread.start()
    return thread


def test_tickets_across_threads() -> None:
    debugger, _journal = make(breakpoints=(N12,), status="paused")
    debugger.enter_pause(pause_info())
    cancel = threading.Event()
    answered: list[ControlTicket] = []
    responses: list[ControlResponse] = []
    client = threading.Thread(target=lambda: responses.append(
        debugger.submit(ControlRequest(action="continue"), timeout=5)))
    client.start()
    time.sleep(0.05)
    assert responses == []
    server = scheduler_answers(debugger, cancel, answered)
    client.join(timeout=5)
    server.join(timeout=5)
    assert len(answered) == 1
    (response,) = responses
    assert response.ok is True
    assert response.message == "continue done"
    assert response.debugger == "running"
    assert response.status == "paused"
    assert response.pause is None
    assert [b.id for b in response.breakpoints] == ["b1"]
    debugger.enter_pause(pause_info())
    do = scheduler_answers(debugger, cancel, answered, result={"value": 1})
    done = debugger.submit(ControlRequest(action="do", op="api.get", args={"path": "/x"}), timeout=5)
    do.join(timeout=5)
    assert done.ok is True
    assert done.result == {"value": 1}
    assert done.debugger == "paused"
    assert done.pause is not None


def test_ticket_timeout_and_state_errors() -> None:
    debugger, _journal = make()
    running = debugger.submit(ControlRequest(action="continue"), timeout=0.1)
    assert running.ok is False
    assert "not paused" in running.message
    debugger.enter_pause(pause_info())
    started = time.monotonic()
    late = debugger.submit(ControlRequest(action="step"), timeout=0.2)
    assert time.monotonic() - started >= 0.2
    assert late.ok is False
    assert late.message == "the run did not answer within 0.2 s"
    assert debugger.next_ticket(threading.Event(), timeout=0.05) is None
    cancel = threading.Event()
    cancel.set()
    assert debugger.next_ticket(cancel) is None
    assert debugger.submit(ControlRequest(action="break_add", breakpoint="foo:bar")).ok is False
    added = debugger.submit(ControlRequest(action="break_add", breakpoint="op:ui.*"))
    assert added.ok is True
    assert [b.op for b in added.breakpoints] == ["ui.*"]
    assert debugger.submit(ControlRequest(action="break_remove", id="b9")).ok is False
    assert debugger.submit(ControlRequest(action="break_remove", id="b1")).ok is True
    assert debugger.submit(ControlRequest(action="break_add")).ok is False


def test_abort_running_and_paused() -> None:
    debugger, _journal = make()
    response = debugger.submit(ControlRequest(action="abort"))
    assert response.ok is True
    assert debugger.abort_requested is True
    assert debugger.state == "aborting"
    assert debugger.before_node(probe(), {}) is None
    paused, _ = make()
    paused.enter_pause(pause_info())
    cancel = threading.Event()
    answered: list[ControlTicket] = []
    server = scheduler_answers(paused, cancel, answered)
    aborted = paused.submit(ControlRequest(action="abort"), timeout=5)
    server.join(timeout=5)
    assert paused.abort_requested is True
    assert [ticket.request.action for ticket in answered] == ["abort"]
    assert aborted.ok is True
    assert aborted.debugger == "aborting"
    off, _ = make(enabled=False)
    assert off.submit(ControlRequest(action="abort")).ok is True
    assert off.abort_requested is True
    assert off.state == "off"


def test_close_answers_pending_tickets() -> None:
    debugger, _journal = make()
    debugger.enter_pause(pause_info())
    responses: list[ControlResponse] = []
    client = threading.Thread(target=lambda: responses.append(
        debugger.submit(ControlRequest(action="retry"), timeout=5)))
    client.start()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not debugger._tickets:
        time.sleep(0.01)
    debugger.close("the run ended: passed")
    client.join(timeout=5)
    (response,) = responses
    assert response.ok is False
    assert response.message == "the run ended: passed"
    assert response.debugger == "off"
    assert debugger.state == "off"
    assert debugger.pause_info is None
    after = debugger.submit(ControlRequest(action="status"))
    assert after.ok is False
    assert after.message == "the run ended: passed"
    assert debugger.next_ticket(threading.Event(), timeout=1) is None
    assert debugger.before_node(probe(), {}) is None


def submit_in_thread(debugger: Debugger, request: ControlRequest, responses: list[ControlResponse],
                     timeout: float = 5) -> threading.Thread:
    thread = threading.Thread(target=lambda: responses.append(debugger.submit(request, timeout=timeout)))
    thread.start()
    return thread


def wait_for_tickets(debugger: Debugger, count: int) -> None:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and len(debugger._tickets) < count:
        time.sleep(0.01)
    assert len(debugger._tickets) == count


def test_tickets_left_from_one_pause_never_reach_the_next() -> None:
    debugger, _journal = make()
    debugger.enter_pause(pause_info())
    first: list[ControlResponse] = []
    second: list[ControlResponse] = []
    step = submit_in_thread(debugger, ControlRequest(action="step"), first)
    wait_for_tickets(debugger, 1)
    skip = submit_in_thread(debugger, ControlRequest(action="skip"), second)
    wait_for_tickets(debugger, 2)
    cancel = threading.Event()
    ticket = debugger.next_ticket(cancel, timeout=1)
    assert ticket is not None
    assert ticket.request.action == "step"
    debugger.answer(ticket, ControlResponse(ok=True, status="running", debugger="stepping", message="stepped"))
    debugger.leave_pause("step")
    step.join(timeout=5)
    skip.join(timeout=5)
    assert first[0].ok is True
    assert second[0].ok is False
    assert second[0].message == "the run resumed before skip was applied"
    assert debugger.before_node(probe(N13), {}) == PauseDecision("step")
    debugger.enter_pause(pause_info("step"))
    assert debugger.next_ticket(cancel, timeout=0.1) is None


def test_enter_pause_answers_tickets_of_an_earlier_pause() -> None:
    debugger, _journal = make()
    debugger.enter_pause(pause_info())
    responses: list[ControlResponse] = []
    client = submit_in_thread(debugger, ControlRequest(action="edit", args={"path": "/x"}), responses)
    wait_for_tickets(debugger, 1)
    debugger.enter_pause(pause_info("error"))
    client.join(timeout=5)
    assert responses[0].ok is False
    assert responses[0].message == "a new pause began before edit was applied"
    assert debugger.next_ticket(threading.Event(), timeout=0.1) is None


def test_a_taken_ticket_is_never_reported_as_unanswered() -> None:
    debugger, _journal = make()
    debugger.enter_pause(pause_info())
    responses: list[ControlResponse] = []
    client = submit_in_thread(debugger, ControlRequest(action="retry"), responses, timeout=0.2)
    wait_for_tickets(debugger, 1)
    ticket = debugger.next_ticket(threading.Event(), timeout=1)
    assert ticket is not None
    client.join(timeout=5)
    assert responses[0].ok is False
    assert responses[0].message.startswith("the run took retry but did not finish it within 0.2 s")
    debugger.answer(ticket, ControlResponse(ok=True, status="running", debugger="paused", message="retried"))
    assert ticket.response is not None
    assert ticket.response.message == "retried"
    withdrawn = debugger.submit(ControlRequest(action="skip"), timeout=0.1)
    assert withdrawn.message == "the run did not answer within 0.1 s"
    assert debugger.next_ticket(threading.Event(), timeout=0.1) is None


def test_close_answers_a_ticket_the_scheduler_took() -> None:
    debugger, _journal = make()
    debugger.enter_pause(pause_info())
    responses: list[ControlResponse] = []
    client = submit_in_thread(debugger, ControlRequest(action="do", op="api.get", args={"path": "/"}), responses)
    wait_for_tickets(debugger, 1)
    ticket = debugger.next_ticket(threading.Event(), timeout=1)
    assert ticket is not None
    debugger.close("the run ended: failed")
    client.join(timeout=5)
    assert responses[0].ok is False
    assert responses[0].message == "the run ended: failed"
    debugger.answer(ticket, ControlResponse(ok=True, status="failed", debugger="off", message="late"))
    assert ticket.response is not None
    assert ticket.response.message == "the run ended: failed"


def test_abort_racing_a_pause_decision_does_not_block_the_scheduler() -> None:
    debugger, _journal = make(breakpoints=(N12,))
    decision = debugger.before_node(probe(), {})
    assert decision is not None
    assert debugger.submit(ControlRequest(action="abort")).ok is True
    debugger.enter_pause(pause_info())
    assert debugger.state == "aborting"
    started = time.monotonic()
    assert debugger.next_ticket(threading.Event(), timeout=2) is None
    assert time.monotonic() - started < 1
    refused = debugger.submit(ControlRequest(action="continue"), timeout=0.1)
    assert refused.ok is False
    assert "not paused" in refused.message


def test_abort_while_paused_drops_queued_actions() -> None:
    debugger, _journal = make()
    debugger.enter_pause(pause_info())
    skipped: list[ControlResponse] = []
    aborted: list[ControlResponse] = []
    skip = submit_in_thread(debugger, ControlRequest(action="skip"), skipped)
    wait_for_tickets(debugger, 1)
    abort = submit_in_thread(debugger, ControlRequest(action="abort"), aborted)
    skip.join(timeout=5)
    assert skipped[0].ok is False
    assert skipped[0].message == "the run is aborting; skip was not applied"
    ticket = debugger.next_ticket(threading.Event(), timeout=1)
    assert ticket is not None
    assert ticket.request.action == "abort"
    debugger.answer(ticket, ControlResponse(ok=True, status="aborted", debugger="aborting", message="aborted"))
    abort.join(timeout=5)
    assert aborted[0].ok is True
    assert aborted[0].message == "aborted"
    assert debugger.next_ticket(threading.Event(), timeout=1) is None


def test_request_abort_while_paused_wakes_the_scheduler() -> None:
    debugger, _journal = make()
    debugger.enter_pause(pause_info())
    results: list[ControlTicket | None] = []
    waiter = threading.Thread(target=lambda: results.append(debugger.next_ticket(threading.Event(), timeout=5)))
    waiter.start()
    time.sleep(0.05)
    debugger.request_abort()
    waiter.join(timeout=2)
    assert results == [None]
    assert debugger.abort_requested is True


def test_label_breakpoint_conditions_see_values_at_source() -> None:
    debugger, journal = make()
    created = debugger.add(f"label:checkout,when:values.{N13}.total > 100")
    assert debugger.at_source(probe(N12, "debug.break"), "checkout", False, {N13: {"total": 50}}) is None
    decision = debugger.at_source(probe(N12, "debug.break"), "checkout", False, {N13: {"total": 150}})
    assert decision == PauseDecision("breakpoint", breakpoint=created.id, label="checkout")
    assert debugger.breakpoints()[0].hits == 1
    assert journal.of("break.error") == []


MATCH_PATTERNS = [
    r"^/api/[a-z],b$", r"a|b", r"(ab)+c", r"^(a+)+$", r"\d{2,3}-\w+", r"[^a-c]x", r"colou?r", r"\bfoo\b", r"\Bo",
    r"x$", r"^$", r"", r"a{,2}b", r"a{2,}", r"a{", r"[]a]", r"[a-]", r"\x41\u00e7", r"(?:ab|cd)*e", r"(?P<n>x)y",
    r".+\n?", r"[\d\s]+", r"\.", r"K.d.k\xf6y", r"a*?b", r"(a|ab)(c|bcd)(d*)", r"[\w-]+@[\w.]+", r"\Aab\Z",
    r"\0", r"\101", r"\N{LATIN SMALL LETTER A}", r"[^\W\d]", r"(a?){3}a{3}", r"()*x", r"(^)*b", r"a{2}|b{1,3}c",
]
MATCH_TEXTS = [
    "", "a", "ab", "abc", "/api/x,b", "aaab", "aaaa", "12-ab", "123-", "dx", "color", "colour", "a foo b", "foobar",
    "xo", "x\n", "x\ny", "]", "-", "A\u00e7", "ababcde", "xy", "\n", "1 2", ".", "Kad\u0131k\u00f6y", "aab", "abcd",
    "x@y.z", "\0", "A", "a{", "aaa", "aaaaaa", "b", "bbc", "\u00e79_",
]


@pytest.mark.parametrize("pattern", MATCH_PATTERNS)
def test_matches_agrees_with_re_search(pattern: str) -> None:
    condition = Condition(f"matches(args.s, {pattern!r})")
    for text in MATCH_TEXTS:
        assert condition.evaluate({"args": {"s": text}}) is (re.search(pattern, text) is not None), (pattern, text)
    assert condition.evaluate({"args": {}}) is False


@pytest.mark.parametrize(
    ("pattern", "fragment"),
    [
        ("(", "missing )"),
        ("a)", "unbalanced parenthesis"),
        ("[ab", "unterminated character set"),
        ("[z-a]", "bad character range"),
        ("*a", "nothing to repeat"),
        ("a**", "multiple repeat"),
        ("^*", "nothing to repeat"),
        ("a*+", "possessive"),
        ("a{2,1}", "minimum repeat"),
        ("a{100000}", "repeat count"),
        ("(a)\\1", "backreference"),
        ("(?P<n>a)(?P=n)", "backreference"),
        ("a(?=b)", "lookahead"),
        ("a(?!b)", "negative lookahead"),
        ("(?<=a)b", "lookbehind"),
        ("(?<!a)b", "negative lookbehind"),
        ("(?>a+)b", "atomic group"),
        ("(?(1)a|b)", "conditional group"),
        ("(?#note)a", "comment group"),
        ("(?i)abc", "inline flag"),
        ("\\q", "bad escape"),
        ("[\\A]", "bad escape"),
        ("\\x4", "hex digits"),
        ("\\N{NO SUCH CHARACTER}", "unknown character name"),
        ("(" * 33 + ")" * 33, "nest deeper"),
        ("(a{100}){10}", "more than 512 steps"),
    ],
)
def test_matches_refuses_patterns(pattern: str, fragment: str) -> None:
    with pytest.raises(ConditionError) as raised:
        Condition(f"matches(args.s, {pattern!r})")
    assert fragment in str(raised.value)
    assert repr(pattern) in str(raised.value)


@pytest.mark.parametrize("text", ["matches(args.s, label)", "matches(args.s, 'a' + 'b')", "matches(args.s, 1)"])
def test_matches_needs_a_constant_pattern(text: str) -> None:
    with pytest.raises(ConditionError, match="constant pattern string"):
        Condition(text)


def test_matches_runs_in_linear_time_without_holding_the_daemon() -> None:
    condition = Condition(r"matches(args.s, '^(a+)+$') or matches(args.s, '(a|a)*c') or matches(args.s, '(\\w*)*!')")
    stop = threading.Event()
    gaps: list[float] = []

    def tick() -> None:
        last = time.monotonic()
        while not stop.is_set():
            time.sleep(0.005)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    ticker = threading.Thread(target=tick)
    ticker.start()
    try:
        started = time.monotonic()
        assert condition.evaluate({"args": {"s": "a" * 4000 + "b"}}) is False
        elapsed = time.monotonic() - started
    finally:
        stop.set()
        ticker.join()
    assert elapsed < 5
    assert gaps and max(gaps) < 0.5


def test_matches_step_budget_raises_condition_error() -> None:
    condition = Condition("matches(args.s, '" + "a?" * 100 + "b')")
    with pytest.raises(ConditionError, match="gave up after 1000000 steps"):
        condition.evaluate({"args": {"s": "a" * 20000}})
    debugger, journal = make(breakpoints=("when:matches(args.s, '" + "a?" * 100 + "b')",))
    assert debugger.before_node(probe(args={"s": "a" * 20000}), {}) is None
    assert [detail["id"] for detail in journal.of("break.error")] == ["b1"]
    assert "gave up" in str(journal.of("break.error")[0]["message"])
