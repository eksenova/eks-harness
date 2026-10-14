from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from eks_harness.cli import main
from eks_harness.data.context import ResourceContext
from eks_harness.docs import REFERENCE_FILES, check_reference, generate, write_reference
from eks_harness.docs.markdown import (
    cell,
    code,
    def_name,
    field_rows,
    field_table,
    sentence,
    table,
    type_text,
)
from eks_harness.docs.ops import builtin_ops, op_schemas, render_op
from eks_harness.docs.resources import CONTEXT_MEMBERS, SEED_HOOKS
from eks_harness.ir.registry import base_registry
from eks_harness.plugins.sdk import BACKEND_HOOKS

REPO = Path(__file__).resolve().parents[1]
REFERENCE = REPO / "docs" / "reference"
EM_DASH = chr(0x2014)


@pytest.fixture(scope="module")
def generated() -> dict[str, str]:
    return generate()


def test_checked_in_reference_matches_the_generator(generated: dict[str, str], tmp_path: Path) -> None:
    written = write_reference(tmp_path)
    assert sorted(path.name for path in written) == sorted(REFERENCE_FILES)
    stale = [name for name in REFERENCE_FILES
             if not (REFERENCE / name).is_file()
             or (REFERENCE / name).read_text(encoding="utf-8") != (tmp_path / name).read_text(encoding="utf-8")]
    assert not stale, f"docs/reference is out of date ({', '.join(stale)}); run: eks-harness docs --out docs/reference"
    assert check_reference(REFERENCE) == []


def test_generation_is_deterministic(generated: dict[str, str]) -> None:
    assert generate() == generated


def test_reference_has_no_machine_paths_or_em_dashes(generated: dict[str, str]) -> None:
    home = str(Path.home())
    for name, text in generated.items():
        assert home not in text, name
        assert EM_DASH not in text, name
        assert text.endswith("\n") and not text.endswith("\n\n"), name


def test_every_builtin_op_is_documented(generated: dict[str, str]) -> None:
    text = generated["ops.md"]
    for spec in builtin_ops(base_registry()):
        assert f"\n### {spec.name}\n" in text


def test_check_reports_differing_and_missing_files(tmp_path: Path) -> None:
    write_reference(tmp_path)
    assert check_reference(tmp_path) == []
    (tmp_path / "ops.md").write_text("stale\n", encoding="utf-8")
    (tmp_path / "cli.md").unlink()
    assert check_reference(tmp_path) == ["cli.md", "ops.md"]


def test_docs_command_check_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["docs", "--out", str(tmp_path)]) == 0
    assert main(["docs", "--out", str(tmp_path), "--check"]) == 0
    (tmp_path / "presets.md").write_text("changed\n", encoding="utf-8")
    capsys.readouterr()
    assert main(["docs", "--out", str(tmp_path), "--check"]) == 1
    assert "presets.md" in capsys.readouterr().out


def test_table_escapes_pipes_and_checks_row_width() -> None:
    assert table(["A", "B"], [("x", cell("a|b"))]) == "| A | B |\n| --- | --- |\n| x | a\\|b |"
    with pytest.raises(ValueError, match="2 cells, expected 3"):
        table(["A", "B", "C"], [("x", "y")])


def test_cell_and_code_flatten_whitespace() -> None:
    assert cell("one\n  two\tthree ") == "one two three"
    assert code("int | None") == "`int \\| None`"
    assert code("a`b") == "``a`b``"
    assert code("") == ""
    assert sentence("element by text") == "Element by text."
    assert sentence("Done.") == "Done."


@pytest.mark.parametrize(("schema", "expected"), [
    ({"type": "string"}, "str"),
    ({"type": "integer"}, "int"),
    ({"type": "array", "items": {"type": "number"}}, "list[float]"),
    ({"type": "array", "prefixItems": [{"type": "integer"}, {"type": "integer"}]}, "tuple[int, int]"),
    ({"type": "object", "additionalProperties": {"type": "boolean"}}, "dict[str, bool]"),
    ({"anyOf": [{"type": "string", "format": "eks-time"}, {"type": "null"}]}, "Time | None"),
    ({"anyOf": [{"type": "string"}, {"$ref": "#/$defs/FmtExpr"}, {"$ref": "#/$defs/RefExpr"}]}, "str | Ref"),
    ({"enum": ["a", "b"]}, '"a" | "b"'),
    ({"const": "zoom"}, '"zoom"'),
    ({"$ref": "#/$defs/eks_harness__ir__ops__overlay__Blur"}, "overlay.Blur"),
    ({}, "any"),
])
def test_type_text(schema: dict[str, object], expected: str) -> None:
    assert type_text(schema) == expected


def test_def_names_are_readable() -> None:
    assert def_name("#/$defs/Keys_float_") == "Keys[float]"
    assert def_name("#/$defs/Keyframe_JsonValue_") == "Keyframe[JSON]"
    assert def_name("JsonValue") == "JSON"
    assert def_name("#/$defs/Target") == "Target"


def test_field_rows_mark_required_and_defaults() -> None:
    schema = {
        "properties": {
            "target": {"$ref": "#/$defs/Target", "description": "Element to tap"},
            "settle": {"type": "boolean", "default": True, "description": "Wait | settle"},
        },
        "required": ["target"],
    }
    assert field_rows(schema) == [
        ("`target`", "`Target`", "yes", "", "Element to tap"),
        ("`settle`", "`bool`", "", "`true`", "Wait \\| settle"),
    ]
    assert field_table({"properties": {}}) == "No arguments."


def test_render_op_lists_summary_arguments_and_examples() -> None:
    registry = base_registry()
    spec = registry.op("ui.tap")
    text = render_op(spec, op_schemas(registry)["ui.tap"], "s.ui.tap")
    assert text.startswith("### ui.tap\n\nTap or click a target.")
    assert "| Python | `s.ui.tap` |" in text
    assert "| Platforms | android, ios, web |" in text
    assert "| `target` | `Target` | yes |  |" in text
    assert "```json" in text


def test_resource_context_members_match_the_class() -> None:
    tree = ast.parse(inspect.getsource(ResourceContext))
    assigned = {node.attr for node in ast.walk(tree)
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self"
                and isinstance(node.ctx, ast.Store) and not node.attr.startswith("_")}
    methods = {name for name, member in vars(ResourceContext).items()
               if not name.startswith("_") and (inspect.isfunction(member) or isinstance(member, property))}
    documented = {name.split("(", 1)[0] for name, _meaning in CONTEXT_MEMBERS}
    assert documented == assigned | methods


def test_seed_hooks_are_backend_hooks() -> None:
    assert {name.split("(", 1)[0] for name, _contract in SEED_HOOKS} <= set(BACKEND_HOOKS)


def test_cookbook_quotes_match_their_sources() -> None:
    from eks_harness.docs.quotes import stale_quotes

    root = Path(__file__).resolve().parents[1]
    assert stale_quotes((root / "docs" / "cookbook.md").read_text(encoding="utf-8"), root) == []


def test_quotes_are_replaced_with_the_current_source(tmp_path: Path) -> None:
    from eks_harness.docs.quotes import stale_quotes, synced

    source = tmp_path / "examples" / "a.py"
    source.parent.mkdir()
    source.write_text("print(1)\n", encoding="utf-8")
    text = "`examples/a.py`:\n\n```python\nprint(0)\n```\n"
    assert stale_quotes(text, tmp_path) == ["examples/a.py"]
    assert synced(text, tmp_path) == "`examples/a.py`:\n\n```python\nprint(1)\n```\n"
