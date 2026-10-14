from __future__ import annotations

from collections.abc import Callable
from typing import Any

import eks
from eks_harness.engine.validator import Issue, validate
from eks_harness.ir.plan import Plan

NOW = "2026-01-15T09:00+03:00"


def flow_plan(body: Callable[[eks.Session], Any]) -> Plan:
    @eks.flow(app="web-app", platform="web", now=NOW, timezone="Europe/Istanbul", locale="tr-TR")
    def probe(s: eks.Session) -> Any:
        return body(s)

    return eks.plan(probe)


def ref_type_issues(body: Callable[[eks.Session], Any]) -> list[Issue]:
    return [issue for issue in validate(flow_plan(body)).issues if issue.code == "ref.type"]


def test_break_when_accepts_bool_and_json_refs_without_ref_type_issues() -> None:
    def body(s: eks.Session) -> None:
        rows = s.record_op("ui.state", {"path": "invoices.items"})
        s.debug.break_("has-rows", when=rows.first)
        s.debug.break_("empty", when=s.ui.exists("#empty"))
        s.debug.break_("flag", when=s.api.get("/api/flags").enabled)
        s.debug.break_("state", when=s.ui.state("invoices.loaded"))

    assert ref_type_issues(body) == []


def test_break_when_rejects_int_ref_from_len_in_validator_as_pyright_does() -> None:
    def body(s: eks.Session) -> None:
        rows = s.record_op("ui.state", {"path": "invoices.items"})
        s.debug.break_("no-rows", when=rows.len())  # pyright: ignore[reportArgumentType]

    found = ref_type_issues(body)
    assert len(found) == 1
    assert "produces int" in found[0].message


def test_break_when_rejects_ui_count_in_validator_as_pyright_does() -> None:
    def body(s: eks.Session) -> None:
        s.debug.break_("x", when=s.ui.count("#x"))  # pyright: ignore[reportArgumentType]

    found = ref_type_issues(body)
    assert len(found) == 1
    assert "produces int" in found[0].message


def test_debug_break_example_from_evidence_checks_validates() -> None:
    def body(s: eks.Session) -> None:
        count = s.record_op("ui.state", {"path": "invoices.items"})
        s.debug.break_("before-submit")
        s.debug.break_("has-rows", when=count.first)
        s.debug.pause_on_error()

    assert ref_type_issues(body) == []
