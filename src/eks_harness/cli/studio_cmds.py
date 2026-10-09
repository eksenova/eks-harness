from __future__ import annotations

import argparse


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("studio", help="publish renders to the studio and open studio links",
                                   description="Video studio commands.")
    sub = parser.add_subparsers(dest="studio_command", required=True, metavar="<command>")
    publish = sub.add_parser("publish", help="list a render in its studio project and upload it as an artifact")
    from eks_harness.studio.publish import configure

    configure(publish)
    publish.set_defaults(func=_publish)
    link = sub.add_parser("link", help="print the studio link of a project or a render")
    link.add_argument("project", help="studio project name")
    link.add_argument("--render", default=None, help="render job id")
    link.set_defaults(func=_link)


def _publish(args: argparse.Namespace) -> int:
    from eks_harness.studio.publish import run

    return run(args)


def _link(args: argparse.Namespace) -> int:
    from urllib.parse import urlencode

    from eks_harness.config import load as load_config

    query = {"project": args.project, **({"render": args.render} if args.render else {})}
    print(f"{load_config().public_url().rstrip('/')}/studio?{urlencode(query)}")
    return 0
