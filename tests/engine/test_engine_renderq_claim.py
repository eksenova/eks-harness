from __future__ import annotations

import os
import threading
import time

import pytest

from eks_harness import renderq


@pytest.fixture
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(renderq, "POLL_SECONDS", 0.02)


def wait_until(condition, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def test_claim_and_drop_are_fifo_across_threads(paths, fast) -> None:
    environ_before = dict(os.environ)
    first = renderq.claim("render", "first", session="run-1")
    assert first.started_at is not None and first.session == "run-1"
    assert renderq.holding(first.id)
    order: list[str] = []
    claimed: dict[str, renderq.Ticket] = {}

    def claimer(name: str) -> None:
        ticket = renderq.claim("render", name, session="run-2")
        order.append(name)
        claimed[name] = ticket

    second = threading.Thread(target=claimer, args=("second",))
    second.start()
    assert wait_until(lambda: renderq.status()["waiting"] == 1)
    third = threading.Thread(target=claimer, args=("third",))
    third.start()
    assert wait_until(lambda: renderq.status()["waiting"] == 2)
    time.sleep(0.15)
    assert order == []
    assert [item["label"] for item in renderq.status()["items"]] == ["first", "second", "third"]
    renderq.drop(first)
    second.join(5)
    assert order == ["second"]
    time.sleep(0.15)
    assert order == ["second"]
    renderq.drop(claimed["second"].id)
    third.join(5)
    assert order == ["second", "third"]
    renderq.drop(claimed["third"])
    assert renderq.status()["items"] == []
    assert dict(os.environ) == environ_before
    assert renderq.HOLDER_ENV not in os.environ


class _Queued(Exception):
    pass


def test_claim_does_not_mark_the_thread_as_holding(paths, fast) -> None:
    ticket = renderq.claim("node", "engine slot")

    def queued(position: int, waiting: renderq.Ticket) -> None:
        raise _Queued(position)

    try:
        with pytest.raises(_Queued), renderq.render_slot("render", "legacy in the same thread", on_wait=queued):
            pytest.fail("the legacy slot must queue behind the claim")
        assert [item["label"] for item in renderq.status()["items"]] == ["engine slot"]
    finally:
        renderq.drop(ticket)
    assert renderq.status()["items"] == []


def test_cancel_removes_the_ticket_and_raises(paths, fast) -> None:
    holder = renderq.claim("render", "holder")
    cancel = threading.Event()
    outcome: dict[str, BaseException] = {}
    positions: list[int] = []

    def waiter() -> None:
        try:
            renderq.claim("blender", "cancelled", session="run-9", cancel=cancel.is_set,
                          on_wait=lambda position, ticket: positions.append(position))
        except RuntimeError as error:
            outcome["error"] = error

    thread = threading.Thread(target=waiter)
    thread.start()
    assert wait_until(lambda: renderq.status()["waiting"] == 1)
    cancel.set()
    thread.join(5)
    assert isinstance(outcome.get("error"), RuntimeError) and str(outcome["error"]) == "cancelled"
    assert positions == [1]
    assert [item["label"] for item in renderq.status()["items"]] == ["holder"]
    renderq.drop(holder)
    assert renderq.status()["items"] == []


def test_cancelled_before_the_first_try_leaves_no_ticket(paths, fast) -> None:
    with pytest.raises(RuntimeError, match="cancelled"):
        renderq.claim("render", "never", cancel=lambda: True)
    assert renderq.status()["items"] == []


def test_on_wait_reports_each_queue_position(paths, fast) -> None:
    holder = renderq.claim("render", "holder")
    ahead = threading.Event()
    seen: list[tuple[int, str]] = []
    tickets: dict[str, renderq.Ticket] = {}

    def first_waiter() -> None:
        tickets["a"] = renderq.claim("render", "a")
        ahead.set()

    def second_waiter() -> None:
        tickets["b"] = renderq.claim("render", "b", on_wait=lambda position, ticket: seen.append((position,
                                                                                                    ticket.label)))

    one = threading.Thread(target=first_waiter)
    one.start()
    assert wait_until(lambda: renderq.status()["waiting"] == 1)
    two = threading.Thread(target=second_waiter)
    two.start()
    assert wait_until(lambda: seen == [(2, "b")])
    renderq.drop(holder)
    assert ahead.wait(5)
    assert wait_until(lambda: seen == [(2, "b"), (1, "b")])
    renderq.drop(tickets["a"])
    two.join(5)
    assert tickets["b"].started_at is not None
    renderq.drop(tickets["b"].id)
    one.join(5)
    assert renderq.status()["items"] == []


def test_drop_of_an_unknown_ticket_is_harmless(paths) -> None:
    renderq.drop("0-0-missing")
    ticket = renderq.claim("render", "x" * 300)
    assert len(ticket.label) == 200
    renderq.drop(ticket)
    renderq.drop(ticket)
    assert renderq.status()["items"] == []


def test_render_slot_still_sets_the_holder_and_cleans_up(paths, fast) -> None:
    previous = os.environ.get(renderq.HOLDER_ENV)
    with renderq.render_slot("render", "legacy", session="feature-x") as ticket:
        assert ticket is not None
        assert os.environ[renderq.HOLDER_ENV] == ticket.id
        assert renderq.current_holder() == ticket.id
        assert renderq.attach_lease("abc123")
        assert renderq.status()["items"][0]["leases"] == ["abc123"]
        with renderq.render_slot("render", "nested") as nested:
            assert nested is None
    assert os.environ.get(renderq.HOLDER_ENV) == previous
    assert renderq.status()["items"] == []
