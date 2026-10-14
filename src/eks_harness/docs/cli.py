from __future__ import annotations

import argparse
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from eks_harness.docs.markdown import anchor, cell, code, page, table

PROG = "eks-harness"
NO_PLUGIN_CLI = "EKS_HARNESS_NO_PLUGIN_CLI"
INTRO = """Every command, subcommand and option of the `eks-harness` CLI, taken from its argparse tree (plugin
commands are left out). `eks-harness help <command> [<subcommand>]` and `--help` print the same help in the
terminal."""


@contextmanager
def without_plugin_commands() -> Iterator[None]:
    previous = os.environ.get(NO_PLUGIN_CLI)
    os.environ[NO_PLUGIN_CLI] = "1"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(NO_PLUGIN_CLI, None)
        else:
            os.environ[NO_PLUGIN_CLI] = previous


def build_parser() -> argparse.ArgumentParser:
    from eks_harness.cli import build_parser as cli_parser

    with without_plugin_commands():
        return cli_parser()


def subcommands(parser: argparse.ArgumentParser) -> tuple[dict[str, argparse.ArgumentParser], dict[str, str]]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            helps = {choice.dest: choice.help or "" for choice in action._choices_actions}
            return dict(action.choices), helps
    return {}, {}


def is_plain_default(value: object) -> bool:
    if isinstance(value, bool) or value is None or value == argparse.SUPPRESS:
        return False
    if isinstance(value, int | float):
        return True
    if isinstance(value, str):
        return bool(value) and not Path(value).is_absolute() and not value.startswith("~")
    return False


def option_name(action: argparse.Action) -> str:
    if action.option_strings:
        names = ", ".join(action.option_strings)
        if action.nargs == 0:
            return names
        metavar = action.metavar or action.dest.upper()
        return f"{names} {metavar if isinstance(metavar, str) else ' '.join(metavar)}"
    metavar = action.metavar or action.dest
    name = metavar if isinstance(metavar, str) else " ".join(metavar)
    if action.nargs in ("?", "*"):
        return f"[{name}]"
    if action.nargs == "+":
        return f"{name} [...]"
    return name


def action_rows(parser: argparse.ArgumentParser) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction | argparse._HelpAction):
            continue
        if action.help == argparse.SUPPRESS:
            continue
        text = cell(action.help or "")
        details = [text if not text or text.endswith((".", "?", "!", ")")) else f"{text}."]
        if action.choices is not None:
            details.append("Choices: " + ", ".join(code(choice) for choice in action.choices) + ".")
        if action.option_strings and is_plain_default(action.default):
            details.append(f"Default: {code(action.default)}.")
        if action.required and action.option_strings:
            details.append("Required.")
        rows.append((code(option_name(action)), " ".join(part for part in details if part)))
    return rows


def walk(parser: argparse.ArgumentParser, path: tuple[str, ...], help_text: str) -> Iterator[str]:
    title = " ".join((PROG, *path))
    parts = [f"{'#' * min(2 + max(len(path) - 1, 0), 4)} {title}"]
    description = (parser.description or "").strip()
    if description and path:
        parts.append(description)
    elif help_text:
        parts.append(cell(help_text))
    rows = action_rows(parser)
    if rows:
        parts.append(table(["Argument", "Help"], rows))
    children, helps = subcommands(parser)
    if children:
        parts.append(table(["Subcommand", "Help"], [
            (f"[{name}](#{anchor(' '.join((PROG, *path, name)))})", cell(helps.get(name, "")))
            for name in sorted(children)
        ]))
    yield "\n\n".join(parts)
    for name in sorted(children):
        yield from walk(children[name], (*path, name), helps.get(name, ""))


def render_cli() -> str:
    parser = build_parser()
    children, helps = subcommands(parser)
    top = [f"## {PROG}", "```text\n" + (parser.description or "").strip() + "\n```"]
    rows = action_rows(parser)
    if rows:
        top.append(table(["Option", "Help"], rows))
    top.append(table(["Command", "Help"], [
        (f"[{name}](#{anchor(f'{PROG} {name}')})", cell(helps.get(name, ""))) for name in sorted(children)
    ]))
    blocks = [INTRO, "\n\n".join(top)]
    for name in sorted(children):
        blocks.extend(walk(children[name], (name,), helps.get(name, "")))
    return page("CLI reference", *blocks)


__all__ = ["build_parser", "render_cli"]
