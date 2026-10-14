from __future__ import annotations

import argparse
from pathlib import Path

from eks_harness.cli.ops_cmds import catalog_registry, find_op
from eks_harness.cli.ui import EXIT_USAGE, fail, print_json, write_line


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "schema", help="export the plan JSON Schema and one schema per op",
        description="Print the plan JSON Schema, or write plan.schema.json and ops/<op>.json into a directory.")
    parser.add_argument("--out", help="directory to write plan.schema.json and ops/<op>.json into")
    parser.add_argument("--op", help="print the argument schema of one op")
    parser.add_argument("--no-plugins", action="store_true", help="do not import the tree's plugin modules")
    parser.add_argument("--json", action="store_true", help="accepted for symmetry; the output is always JSON")
    parser.set_defaults(func=cmd_schema)


def cmd_schema(args: argparse.Namespace) -> int:
    from eks_harness.ir.schema import op_schema, plan_schema, write_schemas

    if args.out and args.op:
        fail("pass --op to print one op's schema or --out to write every schema, not both", EXIT_USAGE)
    registry = catalog_registry(args.no_plugins)
    if args.op:
        print_json(op_schema(find_op(registry, args.op), registry=registry))
        return 0
    if args.out:
        out = Path(args.out).expanduser()
        if out.exists() and not out.is_dir():
            fail(f"--out {out} is not a directory", EXIT_USAGE)
        paths = write_schemas(out, registry=registry)
        if args.json:
            print_json({"paths": [str(path) for path in paths]})
        else:
            for path in paths:
                write_line(str(path))
        return 0
    print_json(plan_schema(registry))
    return 0


__all__ = ["cmd_schema", "register"]
