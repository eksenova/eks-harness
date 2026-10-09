from __future__ import annotations

import argparse

from eks_harness.cli.client import client_from_args
from eks_harness.cli.ui import console, ok, print_json, table


def cmd_queue(args: argparse.Namespace) -> int:
    if args.local:
        from eks_harness import renderq

        data = renderq.status()
    else:
        with client_from_args(args) as client:
            data = client.get("/api/render-queue")
    if args.json:
        print_json(data)
        return 0
    if not data["items"]:
        ok(f"no renders running or waiting (at most {data['concurrency']} at once)")
        return 0
    rows = [[t["state"] if t["state"] == "running" else f"waiting #{t['position']}", t["kind"], t["label"],
             t.get("session") or "", ", ".join(x if isinstance(x, str) else f"{x['sid']} {x['kind']}" for x in t.get("leases") or []), t["pid"],
             f"{t['seconds']:.0f}s"] for t in data["items"]]
    console.print(table(["State", "Kind", "Video", "Session", "Leases", "PID", "For"], rows))
    return 0


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("queue", help="the render queue: video renders and recording encodes, one at a time",
                                   description="Video renders and recording encodes on this machine share one lane and run one at a "
                                               "time (render.concurrency); the rest wait first in, first out.")
    parser.add_argument("--local", action="store_true", help="read the queue files directly (no daemon needed)")
    parser.add_argument("--json", action="store_true", help="print JSON")
    parser.set_defaults(func=cmd_queue)
