from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from pydantic import ValidationError

import eks
from eks import t
from eks_harness.ir.ops.check import Explore
from eks_harness.ir.ops.score import Take
from eks_harness.ir.ops.ui import ReadText
from eks_harness.ir.plan import Node, Plan
from eks_harness.ir.target import Platform
from eks_harness.ir.time import TimeError

NOW = "2026-01-15T09:00+03:00"


def flow_plan(body: Callable[[eks.Session], Any], *, platform: Platform = "web") -> Plan:
    @eks.flow(app="mobile-app" if platform != "web" else "web-app", platforms=[platform], now=NOW,
              timezone="Europe/Istanbul", locale="tr-TR")
    def probe(s: eks.Session) -> Any:
        return body(s)

    return eks.plan(probe)


def nodes_of(plan: Plan, op: str) -> list[Node]:
    return [node for node, _ancestors in plan.walk() if node.op == op]


ANCHORED_LENGTHS: list[tuple[str, Callable[[eks.Session], Any]]] = [
    ("tap timeout", lambda s: s.ui.tap("#a", timeout=t("bar:4"))),
    ("long_press hold", lambda s: s.ui.long_press("#b", hold=t("bar:2"))),
    ("wait_for timeout", lambda s: s.ui.wait_for("#c", timeout=t.end)),
    ("settle timeout", lambda s: s.ui.settle(timeout=t("cue:x"))),
    ("nav timeout", lambda s: s.ui.nav("/a", timeout=t("beat:2"))),
    ("text timeout", lambda s: s.ui.text("#a", timeout=t("bar:1"))),
    ("count timeout", lambda s: s.ui.count("#a", timeout=t("marker:intro"))),
    ("expect timeout", lambda s: eks.expect("#a", timeout=t("bar:1")).visible()),
]


@pytest.mark.parametrize(("what", "body"), ANCHORED_LENGTHS, ids=[what for what, _ in ANCHORED_LENGTHS])
def test_lengths_refuse_points_in_time(what: str, body: Callable[[eks.Session], Any]) -> None:
    with pytest.raises(TimeError, match="point in time"):
        flow_plan(body)


MOBILE_LENGTHS: list[tuple[str, Callable[[eks.Session], Any]]] = [
    ("toggle timeout", lambda s: s.mobile.toggle("#switch", timeout=t("bar:1"))),
    ("submit timeout", lambda s: s.mobile.submit("#amount", timeout=t.end)),
    ("idle timeout", lambda s: s.mobile.idle(timeout=t("beat:1"))),
    ("pointer duration", lambda s: s.mobile.pointer("#a", duration=t("cue:x"))),
]


@pytest.mark.parametrize(("what", "body"), MOBILE_LENGTHS, ids=[what for what, _ in MOBILE_LENGTHS])
def test_mobile_lengths_refuse_points_in_time(what: str, body: Callable[[eks.Session], Any]) -> None:
    with pytest.raises(TimeError, match="point in time"):
        flow_plan(body, platform="ios")


def test_lengths_refuse_zero_and_negative_values() -> None:
    with pytest.raises(TimeError, match="positive length"):
        flow_plan(lambda s: s.ui.tap("#a", timeout=t("0s")))
    with pytest.raises(TimeError, match="positive length"):
        flow_plan(lambda s: s.ui.long_press("#a", hold=t("-1s")))


def test_models_type_lengths_for_hand_written_plans() -> None:
    target = {"by": "test_id", "args": {"id": "a"}}
    with pytest.raises(ValidationError, match="duration_anchor"):
        ReadText.model_validate({"target": target, "timeout": "bar:1"})
    with pytest.raises(ValidationError, match="time_negative"):
        ReadText.model_validate({"target": target, "timeout": "-2s"})
    session = {"key": "take0", "app": "web-app", "platform": "web"}
    # a take's lead-in may be zero, a duration may not
    assert Take.model_validate({"session": session, "lead_in": "0s"}).lead_in == t("0s")
    with pytest.raises(ValidationError, match="duration_zero"):
        Explore.model_validate({"budget": "0s"})


def test_target_reads_take_a_timeout() -> None:
    def body(s: eks.Session) -> None:
        s.ui.text("#a", timeout=t("2s"))
        s.ui.value("#a", timeout=t("2s"))
        s.ui.attr("#a", "href", timeout=t("2s"))
        s.ui.count("#a", timeout=t("2s"))
        s.ui.exists("#a", timeout=t("2s"))
        s.ui.visible("#a", timeout=t("2s"))
        s.ui.layout("#a", timeout=t("2s"))
        s.ui.tree("#a", timeout=t("2s"))

    plan = flow_plan(body)
    reads = [node for node, _ in plan.walk() if node.op.startswith("ui.")]
    assert [node.op for node in reads] == ["ui.text", "ui.value", "ui.attr", "ui.count", "ui.exists", "ui.visible",
                                           "ui.layout", "ui.tree"]
    assert all(node.args["timeout"] == "2s" for node in reads)
    assert eks.validate(plan).issues == ()


def test_exploration_is_declared_as_a_check_op() -> None:
    def body(s: eks.Session) -> None:
        s.explore(t("120s"), depth=4, avoid=["text=Çıkış"], checks=["console_errors"], fail_on=["crash", "page_error"])

    plan = flow_plan(body)
    (explore,) = nodes_of(plan, "check.explore")
    assert explore.args == {
        "budget": "120s", "depth": 4, "allow": [], "avoid": [{"by": "text", "args": {"text": "Çıkış", "exact": True}}],
        "actions": ["tap", "fill", "scroll", "back"], "fail_on": ["crash", "page_error"],
        "checks": [{"kind": "console_errors", "params": {"noise": []}}], "soft": False}
    assert eks.validate(plan).issues == ()
    with pytest.raises(TimeError, match="point in time"):
        flow_plan(lambda s: s.explore(t("bar:1")))
    with pytest.raises(TypeError, match="takes a list"):
        flow_plan(lambda s: s.explore(t("60s"), avoid="#logout"))  # type: ignore[arg-type]
