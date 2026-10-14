from __future__ import annotations

import hashlib
import inspect
import sys
import textwrap
from pathlib import Path

import pytest

from eks.discovery import (
    DiscoveryError,
    EntryFile,
    discover,
    load_file,
    load_tree,
    module_name,
    resolve_entry,
    split_spec,
)
from eks_harness.ir.registry import Registry

FLOWS = """
    import eks

    @eks.flow(app="web-app", platform="web")
    def {first}(s, limit: int = 5):
        return {{"limit": limit}}

    @eks.flow("second-flow", app="web-app", platform="web")
    def second(s):
        return None
"""

ONE_FLOW = """
    import eks

    @eks.flow(app="web-app", platform="web", tags=["{tag}"])
    def only(s):
        return None
"""

SEED = """
    import eks

    @eks.seed("standard", backend="eksenapi")
    def standard(d):
        return None
"""

MIXED = """
    import eks

    @eks.seed("shared", backend="eksenapi")
    def shared_seed(d):
        return None

    @eks.flow("shared", app="web-app")
    def shared_flow(s):
        return None
"""


def write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("spec", "path", "name"),
    [("flows/a.py", "flows/a.py", None), ("flows/a.py::checkout", "flows/a.py", "checkout"),
     ("/abs/x.py::second-flow", "/abs/x.py", "second-flow"), ("a.py::n::", None, None)],
)
def test_split_spec(spec: str, path: str | None, name: str | None) -> None:
    if path is None:
        with pytest.raises(DiscoveryError):
            split_spec(spec)
        return
    assert split_spec(spec) == (Path(path), name)


@pytest.mark.parametrize("spec", ["", "::name", "a.py::", "a.py::  "])
def test_split_spec_errors(spec: str) -> None:
    with pytest.raises(DiscoveryError):
        split_spec(spec)


def test_module_name_is_stable_and_unique(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = tmp_path / "flows" / "check-out.flow.py"
    second = tmp_path / "other" / "check-out.flow.py"
    digest = hashlib.sha256(str(first.resolve()).encode()).hexdigest()[:12]
    assert module_name(first) == f"eks_entry_{digest}_check_out_flow"
    assert module_name(first) == module_name(first)
    assert module_name(first) != module_name(second)
    assert module_name(first).isidentifier()
    monkeypatch.chdir(tmp_path)
    assert module_name(Path("flows/check-out.flow.py")) == module_name(first)
    assert module_name(Path("flows/../flows/check-out.flow.py")) == module_name(first)


def test_load_file_returns_its_entries(tmp_path: Path, eks_registry: Registry) -> None:
    path = write(tmp_path / "flows" / "shop.py", FLOWS.format(first="checkout"))
    entries = load_file(path)
    assert [(e.kind, e.name) for e in entries] == [("flow", "checkout"), ("flow", "second-flow")]
    name = module_name(path)
    assert all(e.module == name for e in entries)
    assert entries[0].file == str(path.resolve())
    assert entries[0].line == 4
    assert eks_registry.entry("flow", "checkout") is entries[0]
    assert name in sys.modules
    assert sys.modules[name].__file__ == str(path.resolve())


def test_loading_twice_uses_the_cache(tmp_path: Path, eks_registry: Registry) -> None:
    path = write(tmp_path / "flows" / "shop.py", FLOWS.format(first="checkout"))
    first = load_file(path)
    module = sys.modules[module_name(path)]
    second = load_file(path)
    assert second == first
    assert all(a is b for a, b in zip(first, second, strict=True))
    assert sys.modules[module_name(path)] is module
    assert len(eks_registry.entries("flow")) >= 2


def test_load_after_restore_registers_again(tmp_path: Path, eks_registry: Registry) -> None:
    path = write(tmp_path / "flows" / "shop.py", FLOWS.format(first="checkout"))
    state = eks_registry.snapshot()
    first = load_file(path)
    module = sys.modules[module_name(path)]
    eks_registry.restore(state)
    with pytest.raises(ValueError, match="unknown flow"):
        eks_registry.entry("flow", "checkout")
    again = load_file(path)
    assert [(e.kind, e.name) for e in again] == [(e.kind, e.name) for e in first]
    assert eks_registry.entry("flow", "checkout") is again[0]
    assert sys.modules[module_name(path)] is not module


def test_restore_to_a_state_holding_the_same_entries_keeps_the_cache(tmp_path: Path, eks_registry: Registry) -> None:
    path = write(tmp_path / "flows" / "shop.py", FLOWS.format(first="checkout"))
    first = load_file(path)
    state = eks_registry.snapshot()
    module = sys.modules[module_name(path)]
    eks_registry.restore(state)
    assert load_file(path) == first
    assert sys.modules[module_name(path)] is module


def test_content_change_executes_again(tmp_path: Path, eks_registry: Registry) -> None:
    path = write(tmp_path / "flows" / "shop.py", FLOWS.format(first="checkout"))
    load_file(path)
    write(path, FLOWS.format(first="pay"))
    entries = load_file(path)
    assert [e.name for e in entries] == ["pay", "second-flow"]
    names = {e.name for e in eks_registry.entries("flow")}
    assert "pay" in names
    assert "checkout" not in names


def test_same_size_rewrite_is_seen(tmp_path: Path, eks_registry: Registry) -> None:
    # Same length and possibly the same mtime second: a bytecode cache would replay the old code.
    path = write(tmp_path / "flows" / "tagged.py", ONE_FLOW.format(tag="aaaa"))
    assert load_file(path)[0].options.model_dump()["tags"] == ("aaaa",)
    write(path, ONE_FLOW.format(tag="bbbb"))
    assert load_file(path)[0].options.model_dump()["tags"] == ("bbbb",)
    assert not (path.parent / "__pycache__").exists()
    del eks_registry


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(DiscoveryError, match="does not exist"):
        load_file(tmp_path / "nope.py")
    with pytest.raises(DiscoveryError, match="does not exist"):
        load_file(tmp_path)


def test_errors_in_the_file_propagate_and_leave_no_module(tmp_path: Path) -> None:
    path = write(tmp_path / "flows" / "broken.py", "import eks\nraise RuntimeError('boom')\n")
    with pytest.raises(RuntimeError, match="boom"):
        load_file(path)
    assert module_name(path) not in sys.modules
    write(path, "def oops(:\n")
    with pytest.raises(SyntaxError):
        load_file(path)


HALF_FLOW = """
    import eks

    @eks.op("acme", name="{op}")
    class Op{op}(eks.Args):
        pass

    @eks.flow(app="web-app", platform="web")
    def {name}(s):
        return None

    {tail}
"""


def test_a_failing_first_load_registers_nothing(tmp_path: Path, eks_registry: Registry) -> None:
    path = write(tmp_path / "flows" / "half.py", HALF_FLOW.format(op="first", name="first", tail="raise RuntimeError('boom')"))
    with pytest.raises(RuntimeError, match="boom"):
        load_file(path)
    assert module_name(path) not in sys.modules
    assert all(e.name != "first" for e in eks_registry.entries())
    assert not eks_registry.has_op("acme.first")


def test_a_failing_edit_keeps_the_previous_version(tmp_path: Path, eks_registry: Registry) -> None:
    path = write(tmp_path / "flows" / "half.py", HALF_FLOW.format(op="second", name="second", tail=""))
    good = load_file(path)
    module = sys.modules[module_name(path)]
    write(path, HALF_FLOW.format(op="third", name="third", tail="raise RuntimeError('boom')"))
    with pytest.raises(RuntimeError, match="boom"):
        load_file(path)
    assert eks_registry.entry("flow", "second") is good[0]
    assert eks_registry.has_op("acme.second")
    assert all(e.name != "third" for e in eks_registry.entries())
    assert not eks_registry.has_op("acme.third")
    assert sys.modules[module_name(path)] is module
    # Fixing the file executes it again.
    write(path, HALF_FLOW.format(op="fourth", name="fourth", tail=""))
    assert [e.name for e in load_file(path)] == ["fourth"]
    assert not eks_registry.has_op("acme.second")
    assert all(e.name != "second" for e in eks_registry.entries())


RENAMED = """
    import eks

    @eks.op("acme", name="thing")
    class {cls}(eks.Args):
        pass

    @eks.preset("acme-{preset}")
    def {preset}():
        from eks_harness.ir.presets import Viewport

        return Viewport(width=100, height=200)

    @eks.flow(app="web-app", platform="web")
    def only(s):
        return None
"""


def test_a_changed_file_may_rename_and_drop_its_registrations(tmp_path: Path, eks_registry: Registry) -> None:
    path = write(tmp_path / "flows" / "renamed.py", RENAMED.format(cls="Foo", preset="kiosk"))
    load_file(path)
    assert eks_registry.op("acme.thing").args.__name__ == "Foo"
    assert eks_registry.preset("viewport", "acme-kiosk").value.model_dump()["width"] == 100
    write(path, RENAMED.format(cls="Bar", preset="booth"))
    assert [e.name for e in load_file(path)] == ["only"]
    assert eks_registry.op("acme.thing").args.__name__ == "Bar"
    assert eks_registry.preset("viewport", "acme-booth").name == "acme-booth"
    assert all(spec.name != "acme-kiosk" for spec in eks_registry.presets("viewport"))


def test_registry_errors_in_the_file_propagate(tmp_path: Path, eks_registry: Registry) -> None:
    write(tmp_path / "a.py", ONE_FLOW.format(tag="a"))
    write(tmp_path / "b.py", ONE_FLOW.format(tag="b"))
    load_file(tmp_path / "a.py")
    with pytest.raises(ValueError, match="flow 'only' is already registered") as raised:
        load_file(tmp_path / "b.py")
    # The message names the colliding source files and lines, not the hashed import names.
    message = str(raised.value)
    first, second = tmp_path.resolve() / "a.py", tmp_path.resolve() / "b.py"
    assert f"{first}:4 (only, from project)" in message
    assert f"{second}:4 (only, from project)" in message
    assert "eks_entry_" not in message
    assert eks_registry.entry("flow", "only").module == module_name(tmp_path / "a.py")


OP_FILE = """
    import eks

    @eks.op("acme", name="ping")
    class Ping{tag}(eks.Args):
        pass
"""


def test_conflicting_ops_in_entry_files_name_the_files(tmp_path: Path, eks_registry: Registry) -> None:
    write(tmp_path / "a.py", OP_FILE.format(tag="A"))
    write(tmp_path / "b.py", OP_FILE.format(tag="B"))
    assert load_file(tmp_path / "a.py") == []
    with pytest.raises(ValueError, match="op 'acme.ping' is already registered") as raised:
        load_file(tmp_path / "b.py")
    message = str(raised.value)
    assert f"{tmp_path.resolve() / 'a.py'}:4 (PingA, from project)" in message
    assert f"{tmp_path.resolve() / 'b.py'}:4 (PingB, from project)" in message
    assert "eks_entry_" not in message


def test_load_functions_read_the_process_registry() -> None:
    # Decorators always write to the one process-wide registry, so the loaders take no registry argument.
    assert list(inspect.signature(load_file).parameters) == ["path"]
    assert "registry" not in inspect.signature(resolve_entry).parameters
    assert "registry" not in inspect.signature(load_tree).parameters


def test_resolve_entry_by_name_and_single_entry(tmp_path: Path, eks_registry: Registry) -> None:
    shop = write(tmp_path / "flows" / "shop.py", FLOWS.format(first="checkout"))
    single = write(tmp_path / "flows" / "single.py", ONE_FLOW.format(tag="x"))
    assert resolve_entry(f"{shop}::second-flow").name == "second-flow"
    assert resolve_entry(f"{shop}::checkout", kind="flow").fn.__name__ == "checkout"
    assert resolve_entry(str(single)).name == "only"
    assert resolve_entry("flows/single.py", cwd=tmp_path).name == "only"
    assert eks_registry.entry("flow", "only").name == "only"


def test_resolve_entry_errors_list_the_names(tmp_path: Path, eks_registry: Registry) -> None:
    shop = write(tmp_path / "flows" / "shop.py", FLOWS.format(first="checkout"))
    with pytest.raises(DiscoveryError, match=r"several flow entries.*checkout \(flow\), second-flow \(flow\)"):
        resolve_entry(str(shop), kind="flow")
    with pytest.raises(DiscoveryError, match=r"shop.py::checkout"):
        resolve_entry(str(shop))
    with pytest.raises(DiscoveryError, match=r"no entry named 'chekout'; did you mean 'checkout'"):
        resolve_entry(f"{shop}::chekout")
    with pytest.raises(DiscoveryError, match=r"it defines checkout \(flow\), second-flow \(flow\)"):
        resolve_entry(f"{shop}::nothing")
    with pytest.raises(DiscoveryError, match="defines no seed"):
        resolve_entry(str(shop), kind="seed")
    empty = write(tmp_path / "flows" / "empty.py", "x = 1\n")
    with pytest.raises(DiscoveryError, match="defines no entry"):
        resolve_entry(str(empty))
    del eks_registry


def test_resolve_entry_same_name_of_two_kinds(tmp_path: Path, eks_registry: Registry) -> None:
    mixed = write(tmp_path / "mixed.py", MIXED)
    with pytest.raises(DiscoveryError, match="several entries named 'shared'"):
        resolve_entry(f"{mixed}::shared")
    assert resolve_entry(f"{mixed}::shared", kind="seed").kind == "seed"
    assert resolve_entry(f"{mixed}::shared", kind="flow").fn.__name__ == "shared_flow"
    del eks_registry


def make_tree(root: Path) -> None:
    harness = root / ".harness"
    write(harness / "project.toml", "[project]\nid = 'acme/shop'\n")
    write(harness / "flows" / "root_flow.py", ONE_FLOW.format(tag="root"))
    write(harness / "flows" / "_helpers.py", "VALUE = 1\n")
    write(harness / "seeds" / "standard.py", SEED)
    write(harness / "edits" / "promo.py", "import eks\n")
    write(harness / "scenes" / "phone.py", "import eks\n")
    write(harness / "scores" / "ad.py", "import eks\n")
    write(harness / "flows" / "notes.txt", "not python\n")
    write(root / "apps" / "web" / ".harness" / "flows" / "b_web.py", FLOWS.format(first="web_checkout"))
    write(root / "apps" / "web" / ".harness" / "flows" / "a_web.py", "import eks\n")
    write(root / "apps" / "mobile" / ".harness" / "flows" / "mobile.py", "import eks\n")
    write(root / "apps" / "web" / "node_modules" / "pkg" / ".harness" / "flows" / "skip.py", "raise SystemExit\n")
    write(root / ".claude" / "worktrees" / "wt" / "app" / ".harness" / "flows" / "skip.py", "raise SystemExit\n")
    write(root / ".venv" / "x" / ".harness" / "flows" / "skip.py", "raise SystemExit\n")


def test_discover_finds_entry_files(tmp_path: Path) -> None:
    make_tree(tmp_path)
    found = discover(tmp_path)
    rel = [(item.path.relative_to(tmp_path.resolve()).as_posix(), item.kind) for item in found]
    assert rel == [
        (".harness/flows/root_flow.py", "flow"),
        (".harness/seeds/standard.py", "seed"),
        (".harness/edits/promo.py", "edit"),
        (".harness/scenes/phone.py", "scene"),
        (".harness/scores/ad.py", "score"),
        ("apps/mobile/.harness/flows/mobile.py", "flow"),
        ("apps/web/.harness/flows/a_web.py", "flow"),
        ("apps/web/.harness/flows/b_web.py", "flow"),
    ]
    assert all(isinstance(item, EntryFile) for item in found)
    assert discover(tmp_path) == found


def test_discover_on_an_empty_tree(tmp_path: Path) -> None:
    assert discover(tmp_path) == []


def test_load_tree_kind_filter(tmp_path: Path, eks_registry: Registry) -> None:
    make_tree(tmp_path)
    seeds = load_tree(tmp_path, kinds=["seed"])
    assert [(e.kind, e.name) for e in seeds] == [("seed", "standard")]
    assert all(e.name != "only" for e in eks_registry.entries("flow"))
    everything = load_tree(tmp_path)
    assert [(e.kind, e.name) for e in everything] == [
        ("flow", "only"), ("seed", "standard"), ("flow", "second-flow"), ("flow", "web_checkout")]
    assert load_tree(tmp_path) == everything
    flows = load_tree(tmp_path, kinds=["flow"])
    assert {e.name for e in flows} == {"only", "second-flow", "web_checkout"}
