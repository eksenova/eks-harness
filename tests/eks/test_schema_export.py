from __future__ import annotations

import copy
import json
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal

import jsonschema
import pytest
from pydantic import BaseModel, Field

import eks
from eks.discovery import load_file, module_name, tree_registry
from eks.workspace import plan_spec
from eks_harness.cli import main
from eks_harness.cli.ui import EXIT_NOT_FOUND, EXIT_USAGE
from eks_harness.ir import SCHEMA_ID
from eks_harness.ir.catalog.effects import EffectBase
from eks_harness.ir.plan import Plan
from eks_harness.ir.registry import Registry, contributing
from eks_harness.ir.schema import (
    OPS_DIR,
    PLAN_SCHEMA_FILE,
    op_schema,
    op_summary,
    plan_schema,
    write_schemas,
)

FIXTURES = Path(__file__).parent / "cli_fixtures"
TYPING_EXAMPLES = Path(__file__).parent / "typing_public_api.py"
FIXTURE_SPECS: list[tuple[str, dict[str, str], str | None]] = [
    (".harness/flows/subeler.py", {}, None),
    (".harness/flows/subeler.py", {"rows": "4", "branch": "Moda"}, None),
    (".harness/flows/kasa.py::kasa", {}, None),
    (".harness/flows/kasa.py::iade", {}, None),
    (".harness/flows/broken.py", {}, None),
    ("mobile-app/.harness/flows/tapper.py", {}, "ios"),
    ("mobile-app/.harness/flows/tapper.py", {}, "android"),
    (".harness/seeds/standard.py", {}, None),
    (".harness/edits/demo.py", {}, None),
    (".harness/scenes/phone.py", {"screen": "artifact:S1"}, None),
]


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "tree"
    shutil.copytree(FIXTURES, root, ignore=shutil.ignore_patterns("__pycache__"))
    monkeypatch.chdir(root)
    monkeypatch.setenv("EKS_HARNESS_NO_PLUGIN_CLI", "1")
    return root


def schema_validator(registry: Registry) -> jsonschema.Draft202012Validator:
    schema = plan_schema(registry)
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


@pytest.fixture
def validator(eks_registry: Registry) -> jsonschema.Draft202012Validator:
    return schema_validator(eks_registry)


def fixture_plans() -> list[Plan]:
    return [plan_spec(spec, params=params, platform=platform) for spec, params, platform in FIXTURE_SPECS]


def example_plans() -> list[Plan]:
    load_file(TYPING_EXAMPLES)
    module = sys.modules[module_name(TYPING_EXAMPLES)]
    return module.typing_plans()


def errors_of(validator: jsonschema.Draft202012Validator, plan: dict[str, Any]) -> list[str]:
    return [error.message for error in validator.iter_errors(plan)]


def subeler_json() -> dict[str, Any]:
    return json.loads(plan_spec(".harness/flows/subeler.py").to_json())


def unhashed(value: Any) -> Any:
    match value:
        case dict():
            return {key: unhashed(item) for key, item in value.items() if key != "hash"}
        case list():
            return [unhashed(item) for item in value]
        case _:
            return value


def refs_in(value: Any) -> Iterator[str]:
    # "examples" holds plan JSON instances, whose {"$ref": node id} objects are values, not schema references
    match value:
        case dict():
            for key, item in value.items():
                if key == "$ref" and isinstance(item, str):
                    yield item
                elif key != "examples":
                    yield from refs_in(item)
        case list():
            for item in value:
                yield from refs_in(item)
        case _:
            return


def test_op_schema_is_titled_described_and_carries_the_summary(eks_registry: Registry) -> None:
    spec = eks_registry.op("ui.tap")
    schema = op_schema(spec)
    assert schema["title"] == "ui.tap"
    assert schema["description"] == "Tap or click a target"
    assert schema["x-eks"] == op_summary(spec)
    assert schema["x-eks"]["name"] == "ui.tap"
    assert schema["x-eks"]["platforms"] == ["android", "ios", "web"]
    assert schema["x-eks"]["pinnable"] is True
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert "target" in schema["properties"]
    assert schema["required"] == ["target"]
    assert schema["$defs"]["Target"]["title"] == "Target"
    assert spec.args.model_validate({"target": {"by": "test_id", "args": {"id": "save"}}})
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate({"target": {"by": "test_id", "args": {"id": "save"}}}, schema,
                        cls=jsonschema.Draft202012Validator)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"target": {"by": "test_id"}, "force": "yes"}, schema,
                            cls=jsonschema.Draft202012Validator)


def test_op_schema_uses_aliases(eks_registry: Registry) -> None:
    aliased = [
        spec for spec in eks_registry.ops()
        if any(field.alias and field.alias != name for name, field in spec.args.model_fields.items())
    ]
    assert aliased
    for spec in aliased:
        properties = op_schema(spec)["properties"]
        for name, field in spec.args.model_fields.items():
            assert (field.alias or name) in properties


def test_op_schema_without_a_description_has_none(eks_registry: Registry) -> None:
    @eks.op("acme")
    class Silent(eks.Args):
        level: int = 1

    schema = op_schema(eks_registry.op("acme.silent"))
    assert schema["title"] == "acme.silent"
    assert "description" not in schema
    assert schema["x-eks"]["plugin"] == "project"


def test_plan_schema_is_draft_2020_12_with_the_plan_id(eks_registry: Registry) -> None:
    schema = plan_schema(eks_registry)
    jsonschema.Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"] == SCHEMA_ID == "eks/plan@1"
    assert schema["type"] == "object"
    assert {"schema", "kind", "name", "nodes", "resources", "clock"} <= set(schema["properties"])
    assert "Plan" not in schema["$defs"]


def test_every_op_has_a_def_and_a_node_variant(eks_registry: Registry) -> None:
    schema = plan_schema(eks_registry)
    defs = schema["$defs"]
    names = [spec.name for spec in eks_registry.ops()]
    for spec in eks_registry.ops():
        assert defs[spec.name]["title"] == spec.name
        assert defs[spec.name]["x-eks"] == op_summary(spec)
    variants = defs["Node"]["oneOf"]
    assert [variant["properties"]["op"]["const"] for variant in variants] == names
    nodes = schema["properties"]["nodes"]["items"]
    assert nodes == {"$ref": "#/$defs/Node"}
    for variant in variants:
        name = variant["properties"]["op"]["const"]
        assert variant["properties"]["args"] == {"$ref": f"#/$defs/{name}"}
        assert variant["properties"]["v"]["const"] == eks_registry.op(name).version
        assert variant["additionalProperties"] is False
        assert "id" in variant["required"]
    when = next(variant for variant in variants if variant["properties"]["op"]["const"] == "ctl.when")
    branch_items = when["properties"]["branches"]["properties"]["then"]["items"]
    assert branch_items == {"$ref": "#/$defs/Node"}


def test_shared_models_live_once_in_defs_and_every_ref_resolves(eks_registry: Registry) -> None:
    schema = plan_schema(eks_registry)
    defs = schema["$defs"]
    refs = set(refs_in(schema))
    for ref in refs:
        assert ref == "#" or (ref.startswith("#/$defs/") and ref.removeprefix("#/$defs/") in defs), ref
    assert "#/$defs/Target" in refs
    assert sum(1 for name in defs if name == "Target" or name.endswith("__Target")) == 1
    assert "#/$defs/Time" not in refs
    model_titles = [body.get("title") for name, body in defs.items() if "x-eks" not in body]
    bodies = [json.dumps(body, sort_keys=True) for body in defs.values()]
    assert len(bodies) == len(set(bodies))
    assert len(model_titles) == len(defs) - len(eks_registry.ops())
    # no model schema is written inline twice: Poly options and shared models are all $refs into $defs
    seen: dict[str, int] = {}

    def inline_models(value: Any) -> Iterator[str]:
        match value:
            case dict():
                if value.get("type") == "object" and isinstance(value.get("properties"), dict) and "title" in value:
                    yield json.dumps(value, sort_keys=True)
                for item in value.values():
                    yield from inline_models(item)
            case list():
                for item in value:
                    yield from inline_models(item)
            case _:
                return

    for body in [*defs.values(), {key: value for key, value in schema.items() if key != "$defs"}]:
        for nested in body.values():
            for text in inline_models(nested):
                seen[text] = seen.get(text, 0) + 1
    def_bodies = set(bodies)
    assert [text for text, count in seen.items() if count > 1] == []
    assert [text for text in seen if text in def_bodies] == []
    # every def is reachable from the root
    reachable: set[str] = set()
    pending = [ref.removeprefix("#/$defs/") for ref in refs_in({k: v for k, v in schema.items() if k != "$defs"})]
    while pending:
        name = pending.pop()
        if name in reachable or name not in defs:
            continue
        reachable.add(name)
        pending.extend(ref.removeprefix("#/$defs/") for ref in refs_in(defs[name]))
    assert reachable == set(defs)


def test_poly_fields_expand_to_every_registered_effect(eks_registry: Registry) -> None:
    @eks.effect(description="Sparkles over the clip")
    class Sparkle(EffectBase, frozen=True):
        kind: Literal["sparkle"] = "sparkle"
        density: float = 1.0

    schema = plan_schema(eks_registry)
    defs = schema["$defs"]
    effect_kinds = {spec.kind for spec in eks_registry.catalogs("effect")}
    assert "sparkle" in effect_kinds
    assert "zoom" in effect_kinds

    def kinds(options: list[Any]) -> set[str]:
        found: set[str] = set()
        for option in options:
            ref = option.get("$ref", "") if isinstance(option, dict) else ""
            body = defs.get(ref.removeprefix("#/$defs/"), {})
            const = body.get("properties", {}).get("kind", {}).get("const")
            if isinstance(const, str):
                found.add(const)
        return found

    def one_ofs(value: Any) -> Iterator[list[Any]]:
        match value:
            case dict():
                if isinstance(value.get("oneOf"), list):
                    yield value["oneOf"]
                for item in value.values():
                    yield from one_ofs(item)
            case list():
                for item in value:
                    yield from one_ofs(item)
            case _:
                return

    expanded = [options for options in one_ofs(schema) if kinds(options) == effect_kinds]
    assert expanded
    for category in ("transition", "easing", "overlay"):
        wanted = {spec.kind for spec in eks_registry.catalogs(category)}
        assert any(kinds(options) == wanted for options in one_ofs(schema)), category


def test_plan_schema_validates_every_fixture_plan(tree: Path, eks_registry: Registry) -> None:
    plans = fixture_plans()
    assert {plan.kind for plan in plans} == {"flow", "seed", "edit", "scene"}
    # the seed's resource kinds are registered in the tree's registry while its file loads, so the schema is
    # built from that registry after planning
    validator = schema_validator(tree_registry(tree))
    for plan in plans:
        assert errors_of(validator, json.loads(plan.to_json())) == [], plan.name


def test_plan_schema_validates_the_design_examples(tree: Path, eks_registry: Registry) -> None:
    plans = example_plans()
    assert {plan.kind for plan in plans} == {"flow", "seed", "edit", "scene", "score"}
    validator = schema_validator(eks_registry)
    for plan in plans:
        assert eks.validate(plan).ok, plan.name
        assert errors_of(validator, json.loads(plan.to_json())) == [], plan.name


def test_plan_schema_rejects_bad_op_args(tree: Path, validator: jsonschema.Draft202012Validator) -> None:
    plan = subeler_json()
    nav = next(node for node in plan["nodes"] if node["op"] == "ui.nav")
    nav["args"]["route"] = 5
    assert errors_of(validator, plan)
    plan = subeler_json()
    nav = next(node for node in plan["nodes"] if node["op"] == "ui.nav")
    nav["args"]["speed"] = "fast"
    assert errors_of(validator, plan)
    plan = subeler_json()
    nav = next(node for node in plan["nodes"] if node["op"] == "ui.nav")
    del nav["args"]["route"]
    assert errors_of(validator, plan)


def test_plan_schema_rejects_bad_nodes(tree: Path, validator: jsonschema.Draft202012Validator) -> None:
    original = subeler_json()
    assert errors_of(validator, original) == []
    cases: list[dict[str, Any]] = []
    for change in ("op", "v", "branches", "extra", "id"):
        plan = copy.deepcopy(original)
        node = next(node for node in plan["nodes"] if node["op"] == "ui.nav")
        match change:
            case "op":
                node["op"] = "ui.teleport"
            case "v":
                node["v"] = "9"
            case "branches":
                node["branches"] = {"then": []}
            case "extra":
                node["colour"] = "red"
            case _:
                node["id"] = "not-an-id"
        cases.append(plan)
    for plan in cases:
        assert errors_of(validator, plan)
    plan = copy.deepcopy(original)
    plan["schema"] = "eks/plan@2"
    assert errors_of(validator, plan)


def test_every_node_variant_requires_op(
    tree: Path, validator: jsonschema.Draft202012Validator, eks_registry: Registry
) -> None:
    schema = plan_schema(eks_registry)
    node_def = schema["$defs"][schema["properties"]["nodes"]["items"]["$ref"].removeprefix("#/$defs/")]
    for variant in node_def["oneOf"]:
        assert "op" in variant["required"], variant["title"]
    original = subeler_json()
    ops = sorted({node["op"] for node in original["nodes"]})
    assert ops
    for op in ops:
        plan = copy.deepcopy(original)
        node = next(node for node in plan["nodes"] if node["op"] == op)
        del node["op"]
        with pytest.raises(ValueError):
            Plan.model_validate(plan)
        assert errors_of(validator, plan), op


def test_plan_schema_checks_branch_items_and_names(tree: Path, validator: jsonschema.Draft202012Validator) -> None:
    plan = json.loads(plan_spec(".harness/flows/kasa.py::kasa").to_json())
    when = next(node for node in plan["nodes"] if node["op"] == "ctl.when")
    assert errors_of(validator, plan) == []
    bad_item = copy.deepcopy(plan)
    tap = next(node for node in bad_item["nodes"] if node["op"] == "ctl.when")["branches"]["then"][0]
    tap["args"]["force"] = "sometimes"
    assert errors_of(validator, bad_item)
    bad_branch = copy.deepcopy(plan)
    next(node for node in bad_branch["nodes"] if node["op"] == "ctl.when")["branches"]["sometimes"] = []
    assert errors_of(validator, bad_branch)
    assert set(when["branches"]) == {"then", "else"}


def test_plugin_ops_are_part_of_the_plan_schema(eks_registry: Registry) -> None:
    @eks.op("acme", out="json", description="Ping the acme widget")
    class Ping(eks.Args):
        target: str = "self"

    schema = plan_schema(eks_registry)
    assert schema["$defs"]["acme.ping"]["properties"]["target"]["default"] == "self"
    assert "acme.ping" in [variant["properties"]["op"]["const"] for variant in schema["$defs"]["Node"]["oneOf"]]


def test_ops_sharing_one_args_model_all_require_args(tree: Path, eks_registry: Registry) -> None:
    class Both(eks.Args):
        level: int

    eks.op("acme", name="one")(Both)
    eks.op("acme", name="two")(Both)

    schema = plan_schema(eks_registry)
    variants = {variant["properties"]["op"]["const"]: variant for variant in schema["$defs"]["Node"]["oneOf"]}
    for name in ("acme.one", "acme.two"):
        assert "args" in variants[name]["required"], name
    # the second op aliases the first op's def instead of repeating it
    assert schema["$defs"]["acme.two"]["$ref"] == "#/$defs/acme.one"

    validator = jsonschema.Draft202012Validator(schema)
    # edited nodes would fail the hash check, so compare unhashed plans
    original = unhashed(subeler_json())
    assert errors_of(validator, original) == []
    assert eks.validate(Plan.model_validate(original), tree=tree).ok
    for name in ("acme.one", "acme.two"):
        with_args = copy.deepcopy(original)
        node = next(node for node in with_args["nodes"] if node["op"] == "ui.nav")
        node["op"] = name
        node["args"] = {"level": 3}
        assert errors_of(validator, with_args) == [], name
        without_args = copy.deepcopy(with_args)
        del next(node for node in without_args["nodes"] if node["op"] == name)["args"]
        assert errors_of(validator, without_args), name
        # the validator agrees with the schema on both plans
        assert eks.validate(Plan.model_validate(with_args), tree=tree).ok, name
        rejected = eks.validate(Plan.model_validate(without_args), tree=tree)
        assert [issue.code for issue in rejected.issues if issue.severity == "error"] == ["args.invalid"], name


def test_plugin_models_never_rename_the_built_in_defs(eks_registry: Registry) -> None:
    before = plan_schema(eks_registry)["$defs"]
    assert "Target" in before
    # names that already collide among built-ins (two Step models, two Blur models) must stay put as well
    qualified_builtins = sorted(name for name in before if name.startswith("eks_harness__"))
    assert qualified_builtins

    with contributing("acme.tools", "1.0"):
        class Target(BaseModel):
            selector: str

        class Step(BaseModel):
            size: int = 1

        class Blur(BaseModel):
            radius: float = 2.0

        @eks.op("acme", description="Aim at a widget")
        class Aim(eks.Args):
            target: Target
            step: Step = Step()
            blur: Blur = Blur()

    after = plan_schema(eks_registry)["$defs"]
    # every built-in def keeps its name and its body; only the node union gains the new variant
    for name, body in before.items():
        if name == "Node":
            continue
        assert name in after, name
        assert after[name] == body, name
    assert after["Target"] == before["Target"]
    # the plugin's models are qualified by the plugin id
    for local in ("Target", "Step", "Blur"):
        assert f"acme.tools.{local}" in after
    aim = after["acme.aim"]
    assert aim["properties"]["target"] == {"$ref": "#/$defs/acme.tools.Target"}
    assert after["acme.tools.Target"]["properties"]["selector"]["type"] == "string"
    # the op file uses the same names as the plan schema
    single = op_schema(eks_registry.op("acme.aim"), registry=eks_registry)
    assert set(single["$defs"]) == {"acme.tools.Target", "acme.tools.Step", "acme.tools.Blur"}
    assert op_schema(eks_registry.op("ui.tap"), registry=eks_registry) == op_schema(eks_registry.op("ui.tap"))


def test_project_models_are_qualified_by_the_project(eks_registry: Registry) -> None:
    class Target(BaseModel):
        selector: str

    @eks.op("acme")
    class Point(eks.Args):
        target: Target

    defs = plan_schema(eks_registry)["$defs"]
    assert defs["acme.point"]["properties"]["target"] == {"$ref": "#/$defs/project.Target"}
    assert "selector" not in defs["Target"]["properties"]


def test_plugin_model_names_never_take_an_op_name(eks_registry: Registry) -> None:
    with contributing("acme", "1.0"):
        # a lower-case model whose plugin-qualified name equals an op name
        class ping(BaseModel):
            host: str

        @eks.op("acme")
        class Ping(eks.Args):
            probe: ping

    defs = plan_schema(eks_registry)["$defs"]
    assert "x-eks" in defs["acme.ping"]
    probe = defs["acme.ping"]["properties"]["probe"]["$ref"].removeprefix("#/$defs/")
    assert probe != "acme.ping"
    assert probe.startswith("acme.")
    assert defs[probe]["properties"]["host"]["type"] == "string"


def variant_of(body: dict[str, Any], key: str, kind: str) -> dict[str, Any]:
    return next(variant for variant in body["oneOf"] if variant["properties"][key]["const"] == kind)


def dispatch_variant(defs: dict[str, Any], owner: str, key: str, kind: str) -> dict[str, Any]:
    return variant_of(defs[owner], key, kind)


def payload_def(defs: dict[str, Any], variant: dict[str, Any], payload: str) -> tuple[str, dict[str, Any]]:
    name = variant["properties"][payload]["$ref"].removeprefix("#/$defs/")
    return name, defs[name]


def dispatched_kinds(body: dict[str, Any], key: str) -> list[str]:
    return [variant["properties"][key]["const"] for variant in body["oneOf"]]


def test_registered_kind_models_are_in_defs_with_descriptions(eks_registry: Registry) -> None:
    @eks.resource("schema_ledger", description="A ledger of a tenant")
    class SchemaLedger(eks.Resource):
        currency: str = Field(description="Currency of the ledger")
        opening: int = Field(default=0, description="Opening balance")

    schema = plan_schema(eks_registry)
    jsonschema.Draft202012Validator.check_schema(schema)
    defs = schema["$defs"]
    described = {
        "ByTestId": ("id",),
        "ByRole": ("role", "name", "exact"),
        "ConsoleErrors": ("noise",),
        "VisualGolden": ("golden", "threshold", "masks"),
    }
    for name, fields in described.items():
        for field in fields:
            assert defs[name]["properties"][field]["description"], (name, field)
    assert payload_def(defs, dispatch_variant(defs, "Target", "by", "test_id"), "args")[0] == "ByTestId"
    assert payload_def(defs, dispatch_variant(defs, "Target", "by", "role"), "args")[0] == "ByRole"
    assert payload_def(defs, dispatch_variant(defs, "CheckUse", "kind", "console_errors"), "params")[0] == "ConsoleErrors"
    assert payload_def(defs, dispatch_variant(defs, "CheckUse", "kind", "visual"), "params")[0] == "VisualGolden"
    assert payload_def(defs, dispatch_variant(defs, "check.media", "kind", "cut"), "params")[0] == "CutCheck"
    _, ledger = payload_def(defs, dispatch_variant(defs, "data.create", "kind", "schema_ledger"), "fields")
    assert ledger["properties"]["currency"]["description"] == "Currency of the ledger"
    assert ledger["properties"]["opening"]["description"] == "Opening balance"
    assert ledger["additionalProperties"] is False
    # one variant per registered kind, and checks only of the families each place accepts
    assert dispatched_kinds(defs["Target"], "by") == [spec.kind for spec in eks_registry.catalogs("target")]
    assert dispatched_kinds(defs["CheckUse"], "kind") == [
        spec.kind for spec in eks_registry.checks() if spec.family in ("checkpoint", "visual", "perf")]
    assert dispatched_kinds(defs["check.media"], "kind") == [spec.kind for spec in eks_registry.checks("media")]
    assert dispatched_kinds(defs["data.create"], "kind") == [spec.kind for spec in eks_registry.resources()]
    assert defs["data.create"]["properties"]["kind"]["enum"] == ["schema_ledger"]
    # a variant requires the payload exactly when its model has a required field
    assert dispatch_variant(defs, "Target", "by", "test_id")["required"] == ["by", "args"]
    assert dispatch_variant(defs, "CheckUse", "kind", "console_errors")["required"] == ["kind"]
    assert dispatch_variant(defs, "CheckUse", "kind", "route")["required"] == ["kind", "params"]
    assert dispatch_variant(defs, "data.create", "kind", "schema_ledger")["required"] == ["kind", "fields"]


def test_op_files_carry_the_kind_dispatch(eks_registry: Registry) -> None:
    @eks.resource("schema_ledger", description="A ledger of a tenant")
    class SchemaLedger(eks.Resource):
        currency: str = Field(description="Currency of the ledger")

    tap = op_schema(eks_registry.op("ui.tap"), registry=eks_registry)
    jsonschema.Draft202012Validator.check_schema(tap)
    assert tap["$defs"]["ByTestId"]["properties"]["id"]["description"]
    assert payload_def(tap["$defs"], dispatch_variant(tap["$defs"], "Target", "by", "role"), "args")[0] == "ByRole"
    checkpoint = op_schema(eks_registry.op("check.checkpoint"), registry=eks_registry)
    assert checkpoint["$defs"]["ConsoleErrors"]["properties"]["noise"]["description"]
    assert checkpoint["$defs"]["VisualGolden"]["properties"]["threshold"]["description"]
    media = op_schema(eks_registry.op("check.media"), registry=eks_registry)
    assert dispatched_kinds(media, "kind") == [spec.kind for spec in eks_registry.checks("media")]
    assert media["$defs"]["CutCheck"]["properties"]["threshold"]["description"]
    create = op_schema(eks_registry.op("data.create"), registry=eks_registry)
    jsonschema.Draft202012Validator.check_schema(create)
    _, ledger = payload_def(create["$defs"], variant_of(create, "kind", "schema_ledger"), "fields")
    assert ledger["properties"]["currency"]["description"] == "Currency of the ledger"
    good = {"kind": "schema_ledger", "key": "main", "path": "schema_ledger:main", "fields": {"currency": "TRY"}}
    jsonschema.validate(good, create, cls=jsonschema.Draft202012Validator)
    for bad in ({**good, "fields": {"curency": "TRY"}}, {**good, "fields": {}}, {**good, "kind": "schema_ledgr"}):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(bad, create, cls=jsonschema.Draft202012Validator)


def test_ops_and_schema_cli_print_the_check_params_models(
    tree: Path, capsys: pytest.CaptureFixture[str], eks_registry: Registry
) -> None:
    assert main(["schema", "--op", "check.checkpoint"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["$defs"]["ConsoleErrors"]["properties"]["noise"]["description"] == "Substrings of messages to ignore"
    assert main(["ops", "check.checkpoint", "--json"]) == 0
    detail = json.loads(capsys.readouterr().out)
    assert detail["schema"]["$defs"]["ConsoleErrors"] == printed["$defs"]["ConsoleErrors"]


def test_misspelled_target_args_check_params_and_resource_fields_fail_the_schema(
    tree: Path, eks_registry: Registry
) -> None:
    flow = subeler_json()
    seed = json.loads(plan_spec(".harness/seeds/standard.py").to_json())
    validator = schema_validator(tree_registry(tree))
    assert errors_of(validator, flow) == []
    assert errors_of(validator, seed) == []

    def target_node(plan: dict[str, Any]) -> dict[str, Any]:
        return next(node for node in plan["nodes"] if node["args"].get("target", {}).get("by") == "test_id")

    def checkpoint(plan: dict[str, Any]) -> dict[str, Any]:
        return next(node for node in plan["nodes"] if node["op"] == "check.checkpoint")

    def create(plan: dict[str, Any]) -> dict[str, Any]:
        return next(node for node in plan["nodes"] if node["op"] == "data.create" and node["args"]["kind"] == "cli_tenant")

    def as_media(plan: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        node = next(node for node in plan["nodes"] if node["op"] == "ui.nav")
        node["op"] = "check.media"
        node["args"] = {"media": {"$ref": plan["nodes"][0]["id"], "path": []}, **args}
        return plan

    assert target_node(flow)["args"]["target"]["args"] == {"id": "invoice-row"}
    valid: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []

    plan = copy.deepcopy(flow)
    target_node(plan)["args"]["target"]["args"] = {"idd": "invoice-row"}
    invalid.append(plan)
    plan = copy.deepcopy(flow)
    target_node(plan)["args"]["target"]["args"] = {}
    invalid.append(plan)
    plan = copy.deepcopy(flow)
    target_node(plan)["args"]["target"]["by"] = "test_idd"
    invalid.append(plan)
    plan = copy.deepcopy(flow)
    target_node(plan)["args"]["target"] = {"by": "role", "args": {"role": "button", "exact": "yes"}}
    invalid.append(plan)
    plan = copy.deepcopy(flow)
    target_node(plan)["args"]["target"] = {"by": "role", "args": {"role": "button", "name": "Kaydet", "exact": True},
                                           "within": {"by": "test_id", "args": {"id": "form"}}}
    valid.append(plan)
    plan = copy.deepcopy(flow)
    target_node(plan)["args"]["target"]["within"] = {"by": "css", "args": {"selectr": "form"}}
    invalid.append(plan)

    plan = copy.deepcopy(flow)
    checkpoint(plan)["args"]["checks"] = [{"kind": "console_errors", "params": {"noise": ["ResizeObserver"]}},
                                          {"kind": "visual", "params": {"threshold": 0.01}}, {"kind": "route", "params": {"expected": "/x"}}]
    valid.append(plan)
    for checks in ([{"kind": "console_errors", "params": {"noize": ["ResizeObserver"]}}],
                   [{"kind": "visual", "params": {"treshold": 0.01}}],
                   [{"kind": "visual", "params": {"threshold": 2}}],
                   [{"kind": "route"}],
                   [{"kind": "console_error"}],
                   [{"kind": "cut"}]):
        plan = copy.deepcopy(flow)
        checkpoint(plan)["args"]["checks"] = checks
        invalid.append(plan)

    valid.append(as_media(copy.deepcopy(flow), {"kind": "cut", "params": {"threshold": 0.2}}))
    valid.append(as_media(copy.deepcopy(flow), {"kind": "loudness", "params": {"lufs": -14}}))
    for args in ({"kind": "cut", "params": {"treshold": 0.2}}, {"kind": "loudness", "params": {}},
                 {"kind": "loudness"}, {"kind": "console_errors"}, {"kind": "cutt"}):
        invalid.append(as_media(copy.deepcopy(flow), args))

    plan = copy.deepcopy(seed)
    create(plan)["args"]["fields"] = {"nmae": "Harman Kuyumculuk"}
    invalid.append(plan)
    plan = copy.deepcopy(seed)
    create(plan)["args"]["fields"] = {"name": 5}
    invalid.append(plan)
    plan = copy.deepcopy(seed)
    create(plan)["args"]["kind"] = "cli_tenantt"
    invalid.append(plan)
    plan = copy.deepcopy(seed)
    create(plan)["args"]["fields"] = {}
    valid.append(plan)

    for plan in valid:
        assert errors_of(validator, plan) == []
    for plan in invalid:
        assert errors_of(validator, plan), json.dumps(plan["nodes"])[:300]


def test_plugin_target_kinds_checks_and_resources_join_the_schema(eks_registry: Registry) -> None:
    with contributing("acme.widgets", "1.0"):
        @eks.target_kind("chip", surfaces=["web"], shorthand="chip", description="A chip by its label")
        class ByChip(eks.Args):
            label: str = Field(description="Label printed on the chip")

        @eks.check("acme_glow", description="Glowing widgets stay below a brightness")
        class AcmeGlow(eks.Args):
            brightness: float = Field(default=0.5, ge=0.0, le=1.0, description="Largest allowed brightness")

        @eks.check("acme_hum", family="media", description="The acme hum is audible")
        class AcmeHum(eks.Args):
            level: float = Field(description="Smallest hum level in dB")

        @eks.resource("acme_shop", description="An acme shop")
        class AcmeShop(eks.Resource):
            city: str = Field(description="City of the shop")

    schema = plan_schema(eks_registry)
    jsonschema.Draft202012Validator.check_schema(schema)
    defs = schema["$defs"]
    chip_name, chip = payload_def(defs, dispatch_variant(defs, "Target", "by", "chip"), "args")
    assert chip_name.startswith("acme.widgets.")
    assert chip["properties"]["label"]["description"] == "Label printed on the chip"
    assert dispatch_variant(defs, "Target", "by", "chip")["description"] == "A chip by its label"
    glow_name, glow = payload_def(defs, dispatch_variant(defs, "CheckUse", "kind", "acme_glow"), "params")
    assert glow_name.startswith("acme.widgets.")
    assert glow["properties"]["brightness"]["description"] == "Largest allowed brightness"
    hum_name, hum = payload_def(defs, dispatch_variant(defs, "check.media", "kind", "acme_hum"), "params")
    assert hum_name.startswith("acme.widgets.")
    assert hum["properties"]["level"]["description"] == "Smallest hum level in dB"
    assert "acme_hum" not in dispatched_kinds(defs["CheckUse"], "kind")
    assert "acme_glow" not in dispatched_kinds(defs["check.media"], "kind")
    shop_name, shop = payload_def(defs, dispatch_variant(defs, "data.create", "kind", "acme_shop"), "fields")
    assert shop_name.startswith("acme.widgets.")
    assert shop["properties"]["city"]["description"] == "City of the shop"

    validator = jsonschema.Draft202012Validator(schema)
    tap = {"id": "n0000000001", "op": "ui.tap", "args": {"target": {"by": "chip", "args": {"label": "Yeni"}}}}
    point = {"id": "n0000000002", "op": "check.checkpoint", "out": "json",
             "args": {"label": "x", "checks": [{"kind": "acme_glow", "params": {"brightness": 0.2}}]}}
    flow = {"schema": "eks/plan@1", "kind": "flow", "name": "x", "nodes": [tap, point]}
    assert errors_of(validator, flow) == []
    shop_node = {"id": "n0000000003", "op": "data.create", "out": "resource:acme_shop",
                 "args": {"kind": "acme_shop", "key": "kadikoy", "path": "acme_shop:kadikoy", "fields": {"city": "Istanbul"}}}
    assert errors_of(validator, {"schema": "eks/plan@1", "kind": "seed", "name": "x", "nodes": [shop_node]}) == []
    for path, value in ((("nodes", 0, "args", "target", "args"), {"lable": "Yeni"}),
                        (("nodes", 1, "args", "checks", 0, "params"), {"brigthness": 0.2}),
                        (("nodes", 1, "args", "checks", 0, "params"), {"brightness": 3})):
        bad = copy.deepcopy(flow)
        holder: Any = bad
        for step in path[:-1]:
            holder = holder[step]
        holder[path[-1]] = value
        assert errors_of(validator, bad), path
    bad_shop = copy.deepcopy(shop_node)
    bad_shop["args"]["fields"] = {"cty": "Istanbul"}
    assert errors_of(validator, {"schema": "eks/plan@1", "kind": "seed", "name": "x", "nodes": [bad_shop]})
    # the single op files see the plugin kinds as well
    tap_file = op_schema(eks_registry.op("ui.tap"), registry=eks_registry)
    assert chip_name in tap_file["$defs"]
    jsonschema.validate({"target": {"by": "chip", "args": {"label": "Yeni"}}}, tap_file,
                        cls=jsonschema.Draft202012Validator)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"target": {"by": "chip", "args": {"lable": "Yeni"}}}, tap_file,
                            cls=jsonschema.Draft202012Validator)


def test_without_registered_resources_no_create_validates(eks_registry: Registry) -> None:
    assert eks_registry.resources() == []
    create = op_schema(eks_registry.op("data.create"), registry=eks_registry)
    jsonschema.Draft202012Validator.check_schema(create)
    assert create["properties"]["kind"]["enum"] == []
    assert "oneOf" not in create
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"kind": "tenant", "path": "tenant:x"}, create, cls=jsonschema.Draft202012Validator)


def test_plan_schema_is_deterministic(eks_registry: Registry) -> None:
    first = json.dumps(plan_schema(eks_registry), ensure_ascii=False)
    second = json.dumps(plan_schema(eks_registry), ensure_ascii=False)
    assert first == second


def test_write_schemas_writes_the_plan_and_one_file_per_op(tmp_path: Path, eks_registry: Registry) -> None:
    out = tmp_path / "schemas"
    paths = write_schemas(out, registry=eks_registry)
    assert paths == sorted(paths)
    expected = {out / PLAN_SCHEMA_FILE} | {out / OPS_DIR / f"{spec.name}.json" for spec in eks_registry.ops()}
    assert set(paths) == expected
    assert len(paths) == len(eks_registry.ops()) + 1
    assert json.loads((out / PLAN_SCHEMA_FILE).read_text(encoding="utf-8")) == plan_schema(eks_registry)
    tap = json.loads((out / OPS_DIR / "ui.tap.json").read_text(encoding="utf-8"))
    assert tap == op_schema(eks_registry.op("ui.tap"))
    assert sorted(path.name for path in (out / OPS_DIR).iterdir()) == sorted(
        f"{spec.name}.json" for spec in eks_registry.ops())


def test_write_schemas_twice_is_byte_identical(tmp_path: Path, eks_registry: Registry) -> None:
    first = write_schemas(tmp_path / "a", registry=eks_registry)
    second = write_schemas(tmp_path / "b", registry=eks_registry)
    assert [path.relative_to(tmp_path / "a") for path in first] == [path.relative_to(tmp_path / "b") for path in second]
    for left, right in zip(first, second, strict=True):
        assert left.read_bytes() == right.read_bytes()
    again = write_schemas(tmp_path / "a", registry=eks_registry)
    assert again == first
    for left, right in zip(again, second, strict=True):
        assert left.read_bytes() == right.read_bytes()


def test_schema_cli_prints_the_plan_schema(tree: Path, capsys: pytest.CaptureFixture[str],
                                           eks_registry: Registry) -> None:
    assert main(["schema"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == plan_schema(eks_registry)


def test_schema_cli_prints_one_op(tree: Path, capsys: pytest.CaptureFixture[str], eks_registry: Registry) -> None:
    assert main(["schema", "--op", "ui.tap"]) == 0
    assert json.loads(capsys.readouterr().out) == op_schema(eks_registry.op("ui.tap"))
    assert main(["schema", "--op", "ui.tapp"]) == EXIT_NOT_FOUND
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "'ui.tap'" in captured.err


def test_schema_cli_writes_files_and_prints_the_paths(
    tree: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], eks_registry: Registry
) -> None:
    out = tmp_path / "exported"
    assert main(["schema", "--out", str(out)]) == 0
    printed = capsys.readouterr().out.splitlines()
    expected = write_schemas(tmp_path / "direct", registry=eks_registry)
    assert printed == [str(out / path.relative_to(tmp_path / "direct")) for path in expected]
    assert printed[0].endswith(f"ops/{eks_registry.ops()[0].name}.json")
    assert str(out / PLAN_SCHEMA_FILE) in printed
    for path in printed:
        assert Path(path).is_file()
    assert main(["schema", "--out", str(out), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"paths": printed}


def test_schema_cli_rejects_op_with_out(tree: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["schema", "--op", "ui.tap", "--out", str(tmp_path / "x")]) == EXIT_USAGE
    assert not (tmp_path / "x").exists()
    capsys.readouterr()
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    assert main(["schema", "--out", str(blocker)]) == EXIT_USAGE
    assert "not a directory" in capsys.readouterr().err
