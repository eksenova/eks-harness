from __future__ import annotations

import argparse
import json
import linecache
import sys
import sysconfig
import traceback
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

from eks_harness.cli.ops_cmds import plain_rows, wide_console
from eks_harness.cli.ui import EXIT_USAGE, CliExit, error, print_json, table, write_line

if TYPE_CHECKING:
    from eks_harness.engine.validator import Report
    from eks_harness.ir.plan import Node, Plan

PLAN_INVALID = 1
ARGS_WIDTH = 120
HASH_DIGITS = 16
INDENT = "  "
ELLIPSIS = "..."
HASH_PREFIX = "sha256:"
LIBRARY_PATH_KEYS = ("stdlib", "platstdlib", "purelib", "platlib")


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "plan", help="build and validate the plan of a flow, seed, edit, scene or score",
        description="Trace a flow, seed, edit, scene or score file into its plan (no IO, nothing runs), validate it "
                    "and print the nodes, or the plan and the validation report as JSON. Exit 1 when the plan has "
                    "validation errors.")
    parser.add_argument("spec", help="file.py or file.py::name")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                        help="a parameter of the entry function, parsed into its annotated type (repeatable)")
    parser.add_argument("--platform", choices=["web", "ios", "android"],
                        help="the platform of a flow (default: the flow's or the app's only platform)")
    parser.add_argument("--no-plugins", action="store_true", help="do not import the tree's plugin modules")
    parser.add_argument("--json", action="store_true", help="print the plan and the validation report as JSON")
    parser.set_defaults(func=cmd_plan)


def _authoring_errors() -> tuple[type[BaseException], ...]:
    from pydantic import ValidationError

    from eks.discovery import DiscoveryError
    from eks.planning import PlanBuildError
    from eks.ref import RefUsageError
    from eks.tracing import TraceError
    from eks_harness.ir.plan import PlanError
    from eks_harness.ir.refs import RefError
    from eks_harness.ir.registry import RegistryError
    from eks_harness.ir.target import TargetError
    from eks_harness.ir.time import TimeError
    from eks_harness.ir.types import RefTypeError

    return (DiscoveryError, PlanBuildError, TraceError, RegistryError, ValidationError, TimeError, TargetError,
            RefError, RefUsageError, RefTypeError, PlanError, SyntaxError)


def _chain(problem: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    current: BaseException | None = problem
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _real_file(filename: str) -> bool:
    return bool(filename) and not filename.startswith("<")


def _inside(path: Path, roots: Sequence[Path]) -> bool:
    return any(path == root or path.is_relative_to(root) for root in roots)


def _shown(path: Path, cwd: Path) -> str:
    return str(path.relative_to(cwd)) if path.is_relative_to(cwd) else str(path)


def _library_roots() -> list[Path]:
    import eks
    import eks_harness

    paths = sysconfig.get_paths()
    candidates = [Path(eks.__file__).parent, Path(eks_harness.__file__).parent,
                  *(Path(paths[key]) for key in LIBRARY_PATH_KEYS if key in paths),
                  Path(sys.prefix), Path(sys.base_prefix), Path(sys.exec_prefix)]
    return sorted({candidate.resolve() for candidate in candidates})


@dataclass(frozen=True, slots=True)
class Entry:
    file: Path | None
    roots: tuple[Path, ...]
    tree: Path | None = None


@dataclass(frozen=True, slots=True)
class Spot:
    path: Path
    line: int
    text: str

    def shown(self, cwd: Path) -> str:
        where = f"{_shown(self.path, cwd)}:{self.line}"
        return f"{where}: {self.text}" if self.text else where


def _user_spots(problem: BaseException, entry: Entry) -> list[Spot]:
    library = _library_roots()
    spots: list[Spot] = []
    for frame in traceback.extract_tb(problem.__traceback__):
        if not _real_file(frame.filename):
            continue
        path = Path(frame.filename).resolve()
        if _inside(path, library) or not _inside(path, entry.roots):
            continue
        line = frame.lineno or 0
        spots.append(Spot(path, line, (frame.line or linecache.getline(str(path), line)).strip()))
    return spots


def _syntax_spot(problem: BaseException) -> Spot | None:
    if not isinstance(problem, SyntaxError) or not problem.filename or not _real_file(problem.filename):
        return None
    return Spot(Path(problem.filename).resolve(), problem.lineno or 0, (problem.text or "").strip())


def _source_line(problem: BaseException, entry: Entry, cwd: Path) -> str | None:
    for link in _chain(problem):
        spots = _user_spots(link, entry)
        if spots:
            innermost = spots[-1]
            located = innermost.shown(cwd)
            callers = [spot for spot in spots if spot.path == entry.file]
            if entry.file is not None and innermost.path != entry.file and callers:
                located = f"{located}\n  called from {callers[-1].shown(cwd)}"
            return located
        spot = _syntax_spot(link)
        if spot is not None:
            return spot.shown(cwd)
    return None


def _message(problem: BaseException, authoring: bool) -> str:
    text = str(problem).strip()
    if authoring and text and not isinstance(problem, SyntaxError):
        return text
    return f"{type(problem).__name__}: {text}" if text else type(problem).__name__


def _entry(spec: str, cwd: Path) -> Entry:
    from eks.discovery import DiscoveryError, find_tree, split_spec

    try:
        path, _name = split_spec(spec)
    except DiscoveryError:
        return Entry(None, (cwd.resolve(),))
    path = path.expanduser()
    file = (path if path.is_absolute() else cwd / path).resolve()
    tree = find_tree(file.parent)
    roots = [file.parent]
    if tree is not None:
        roots.append(tree.resolve())
    return Entry(file, tuple(roots), tree)


def _report(problem: BaseException, where: str | None, authoring: bool) -> NoReturn:
    message = _message(problem, authoring)
    raise CliExit(EXIT_USAGE, f"{message}\n  at {where}" if where else message) from problem


def _build(args: argparse.Namespace, cwd: Path) -> tuple[Plan, Report]:
    import eks
    from eks.planning import parse_sets
    from eks.workspace import plan_spec, workspace_registry

    errors = _authoring_errors()
    try:
        params = parse_sets(args.set)
        plan = plan_spec(args.spec, params=params, platform=args.platform, cwd=cwd, plugins=not args.no_plugins)
    except errors as problem:
        _report(problem, _source_line(problem, _entry(args.spec, cwd), cwd), authoring=True)
    except Exception as problem:
        where = None if getattr(args, "debug", False) else _source_line(problem, _entry(args.spec, cwd), cwd)
        if where is None:
            raise
        _report(problem, where, authoring=False)
    tree = _entry(args.spec, cwd).tree
    registry = workspace_registry(tree, plugins=not args.no_plugins) if tree is not None else None
    return plan, eks.validate(plan, registry=registry, tree=tree)


def _walk(nodes: Sequence[Node], depth: int, branch: str | None) -> Iterator[tuple[Node, int, str | None]]:
    for node in nodes:
        yield node, depth, branch
        for name, children in (node.branches or {}).items():
            yield from _walk(children, depth + 1, name)


def _summary(value: Any) -> str:
    if not value:
        return ""
    text = json.dumps(value, ensure_ascii=False, separators=(", ", ": "), sort_keys=True)
    return text if len(text) <= ARGS_WIDTH else text[:ARGS_WIDTH - len(ELLIPSIS)] + ELLIPSIS


def _short_hash(value: str) -> str:
    return value.removeprefix(HASH_PREFIX)[:HASH_DIGITS] or "-"


def _header(plan: Plan, report: Report) -> str:
    count = sum(1 for _ in plan.walk())
    plan_hash = plan.hash or report.plan_hash
    return (f"{plan.kind} {plan.name}: {count} node{'' if count == 1 else 's'}, {len(plan.resources)} "
            f"resource{'' if len(plan.resources) == 1 else 's'}, hash {_short_hash(plan_hash)}")


def _print_text(plan: Plan, report: Report) -> None:
    write_line(_header(plan, report))
    rows = [
        (f"{INDENT * depth}{node.id}", node.op if branch is None else f"{node.op} [{branch}]", _summary(node.args),
         ", ".join(node.uses), ", ".join(node.after), node.src or "")
        for node, depth, branch in _walk(plan.nodes, 0, None)
    ]
    columns = [("id", {"no_wrap": True}), ("op", {"no_wrap": True}), ("args", {"overflow": "fold"}),
               ("uses", {"no_wrap": True}), ("after", {"no_wrap": True}), ("src", {"no_wrap": True})]
    wide_console().print(table(columns, plain_rows(rows)))
    if report.issues:
        for line in report.text().splitlines():
            write_line(line)
    else:
        write_line("valid: no issues")
    if not report.ok:
        count = len(report.errors)
        error(f"{plan.kind} {plan.name} has {count} validation error{'' if count == 1 else 's'}")


def cmd_plan(args: argparse.Namespace) -> int:
    plan, report = _build(args, Path.cwd())
    if args.json:
        print_json({"plan": json.loads(plan.to_json(indent=None)), "validation": report.as_dict()})
    else:
        _print_text(plan, report)
    return 0 if report.ok else PLAN_INVALID


__all__ = ["PLAN_INVALID", "cmd_plan", "register"]
