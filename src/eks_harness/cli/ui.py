from __future__ import annotations

import json
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, NoReturn

from prompt_toolkit import prompt
from prompt_toolkit.application.current import get_app
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.shortcuts import choice
from prompt_toolkit.styles import Style
from prompt_toolkit.validation import ValidationError, Validator
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_DAEMON_DOWN = 3
EXIT_UNAUTHORIZED = 4
EXIT_FORBIDDEN = 5
EXIT_NOT_FOUND = 6
EXIT_GONE = 7
EXIT_CONFLICT = 8
EXIT_NO_CREDENTIALS = 10
EXIT_NOTHING_SELECTED = 11
EXIT_INTERRUPTED = 130

GOLD = "#d4a82a"
NAVY = "#0b1f3a"
BORDER = "#1f3a60"
HINT = "#8a93a3"
SUGGESTION_LIMIT = 100

PROMPT_STYLE = Style.from_dict({
    "accent": f"{GOLD} bold",
    "hint": HINT,
    "completion-menu.completion": "bg:#1b2a41 #e8e6e1",
    "completion-menu.completion.current": f"bg:{GOLD} {NAVY} bold",
    "completion-menu.meta.completion": "bg:#14213a #9aa4b2",
    "completion-menu.meta.completion.current": f"bg:#b8911f {NAVY}",
})

ASCII_FOLD = str.maketrans("çğıöşüâîûÇĞİÖŞÜÂÎÛ", "cgiosuaiuCGIOSUAIU")

console = Console(highlight=False)
err = Console(highlight=False, stderr=True)


class CliExit(Exception):
    def __init__(self, code: int, message: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def normalize(text: str) -> str:
    return (text or "").translate(ASCII_FOLD).casefold()


def interactive() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def fail(message: str, code: int = EXIT_ERROR) -> NoReturn:
    raise CliExit(code, message)


def ok(message: str) -> None:
    err.print(f"[bold green]✓[/] {message}")


def warn(message: str) -> None:
    err.print(f"[bold yellow]![/] {message}")


def error(message: str) -> None:
    err.print(f"[bold red]✗[/] {message}")


def info(message: str) -> None:
    err.print(f"[{HINT}]{message}[/]")


def print_json(data: Any) -> None:
    sys.stdout.write(json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n")
    sys.stdout.flush()


def print_links(links: dict[str, Any], keys: Sequence[str] = ("rawUrl", "url", "sessionUrl")) -> None:
    for key in keys:
        value = links.get(key)
        if value:
            sys.stdout.write(f"{value}\n")
    sys.stdout.flush()


def kv_panel(title: str, rows: Iterable[tuple[str, Any]]) -> Panel:
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    for key, value in rows:
        table.add_row(str(key), "" if value is None else str(value))
    return Panel(table, title=f"[bold]{title}[/]", border_style=BORDER, box=box.ROUNDED)


def table(columns: Sequence[str | tuple[str, dict]], rows: Iterable[Sequence[Any]], title: str | None = None) -> Table:
    result = Table(title=title, box=box.SIMPLE_HEAD, header_style="bold", border_style=BORDER)
    for column in columns:
        if isinstance(column, tuple):
            result.add_column(column[0], **column[1])
        else:
            result.add_column(column)
    for row in rows:
        result.add_row(*("" if value is None else str(value) for value in row))
    return result


def human_size(size: int | float | None) -> str:
    if size is None:
        return ""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


@dataclass(frozen=True)
class Suggestion:
    value: str
    meta: str = ""
    search: str = ""

    @classmethod
    def of(cls, value: str, meta: str = "", *extra: str) -> "Suggestion":
        return cls(value=value, meta=meta, search=normalize(" ".join([value, meta, *extra])))


class SuggestionCompleter(Completer):
    def __init__(self, suggestions: Sequence[Suggestion], limit: int = SUGGESTION_LIMIT) -> None:
        self.suggestions = list(suggestions)
        self.limit = limit

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        tokens = normalize(text).split()
        shown = 0
        for suggestion in self.suggestions:
            if tokens and not all(token in (suggestion.search or normalize(suggestion.value)) for token in tokens):
                continue
            yield Completion(suggestion.value, start_position=-len(text), display=suggestion.value,
                             display_meta=suggestion.meta)
            shown += 1
            if shown >= self.limit:
                return


class ChoiceValidator(Validator):
    def __init__(self, values: Iterable[str], allow_other: bool = False, message: str = "pick one of the suggestions") -> None:
        self.values = {v for v in values}
        self.allow_other = allow_other
        self.message = message

    def validate(self, document) -> None:
        text = document.text.strip()
        if not text:
            raise ValidationError(message="required")
        if not self.allow_other and text not in self.values:
            raise ValidationError(message=self.message)


class FunctionValidator(Validator):
    def __init__(self, check: Callable[[str], str | None]) -> None:
        self.check = check

    def validate(self, document) -> None:
        problem = self.check(document.text.strip())
        if problem:
            raise ValidationError(message=problem)


def open_completions() -> None:
    get_app().current_buffer.start_completion(select_first=False)


def require_interactive(what: str, code: int = EXIT_USAGE) -> None:
    if not interactive():
        fail(f"{what} is required and there is no terminal to ask on; pass it as a flag", code)


def ask_text(label: str, hint: str = "", default: str = "", completer: Completer | None = None,
             validator: Validator | None = None, is_password: bool = False, show_suggestions: bool = False) -> str:
    message = f"<accent>{label}</accent>"
    if hint:
        message += f" <hint>({hint})</hint>"
    return prompt(
        HTML(message + " › "),
        default=default,
        completer=completer,
        complete_while_typing=completer is not None,
        validator=validator,
        validate_while_typing=False,
        is_password=is_password,
        style=PROMPT_STYLE,
        pre_run=open_completions if show_suggestions else None,
    ).strip()


def ask_choice(label: str, options: list[tuple[str, str]], default: str) -> str:
    return choice(message=HTML(f"<b>{label}</b>"), options=options, default=default, style=PROMPT_STYLE)


def ask_confirm(label: str, default: bool = False) -> bool:
    return ask_choice(label, [("yes", "Yes"), ("no", "No")], "yes" if default else "no") == "yes"


def ask_suggested(label: str, suggestions: Sequence[Suggestion], hint: str = "", default: str = "",
                  allow_other: bool = False) -> str:
    return ask_text(label, hint, default, completer=SuggestionCompleter(suggestions),
                    validator=ChoiceValidator([s.value for s in suggestions], allow_other=allow_other),
                    show_suggestions=True)


def ask_new_password(label: str = "Password", minimum: int = 8) -> str:
    while True:
        first = ask_text(label, f"at least {minimum} characters, not shown", is_password=True,
                         validator=FunctionValidator(lambda t: None if len(t) >= minimum else f"at least {minimum} characters"))
        second = ask_text(f"{label} again", "not shown", is_password=True)
        if first == second:
            return first
        error("the passwords do not match; try again")


def confirm_or_exit(label: str, assume_yes: bool) -> None:
    if assume_yes:
        return
    if not interactive():
        fail(f"{label}: pass --yes to confirm without a terminal", EXIT_USAGE)
    if not ask_confirm(label, default=False):
        fail("cancelled", EXIT_NOTHING_SELECTED)
