from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from typing import Any

import pytest
from pydantic import JsonValue

from eks_harness.engine.errors import NodeFailure, OutputTypeError, RefResolutionError
from eks_harness.engine.values import (
    PAUSE_VALUES_BYTES,
    ResolvedArgs,
    Segment,
    ValueStore,
    checked_output,
    child_instance,
    dynamic_segment,
    each_segment,
    fmt_text,
    instance_prefixes,
    instance_segments,
    output_problem,
    resolve,
    resolve_args,
    truthy,
    validate_args,
    value_digest,
)
from eks_harness.ir.hashing import canonical_json, digest
from eks_harness.ir.ops.api import Post
from eks_harness.ir.plan import Node
from eks_harness.ir.refs import FindStep
from eks_harness.ir.registry import Registry, default_registry
from eks_harness.ir.types import Artifact, MediaInfo

N1 = "n0000000001"
N2 = "n0000000002"
N3 = "n0000000003"
E1 = "n00000000e1"
E2 = "n00000000e2"
D7 = "n00000000d7"


@pytest.fixture(autouse=True)
def registry() -> Iterator[Registry]:
    chosen = default_registry()
    state = chosen.snapshot()
    try:
        yield chosen
    finally:
        chosen.restore(state)


def ref(node: str, *path: Any) -> dict[str, JsonValue]:
    expr: dict[str, JsonValue] = {"$ref": node}
    if path:
        expr["path"] = list(path)
    return expr


def node_of(op: str, args: dict[str, JsonValue], out: str = "json", node_id: str = N3) -> Node:
    return Node(id=node_id, op=op, args=args, out=out)


def test_instance_segments_and_helpers() -> None:
    assert child_instance("", "a#0") == "a#0"
    assert child_instance("a#0", "b") == "a#0/b"
    assert each_segment("n00000000e1", 3) == "n00000000e1#3"
    assert dynamic_segment(D7) == D7
    key = child_instance(child_instance(child_instance("", each_segment("e1", 0)), dynamic_segment("d7")),
                         each_segment("e2", 3))
    assert key == "e1#0/d7/e2#3"
    assert instance_segments(key) == [Segment("e1", 0), Segment("d7", None), Segment("e2", 3)]
    assert instance_segments("") == []
    assert instance_prefixes("a#1/b") == ["a#1/b", "a#1", ""]
    assert instance_prefixes("") == [""]
    with pytest.raises(ValueError, match="invalid instance segment"):
        child_instance("a#0", "")
    with pytest.raises(ValueError):
        child_instance("", "a/b")
    with pytest.raises(ValueError):
        each_segment("e1", -1)


@pytest.mark.parametrize("key", ["a#x", "a#-1", "/a", "a//b", "a#", "#1", "a/", "a#01"])
def test_malformed_instance_keys(key: str) -> None:
    with pytest.raises(ValueError):
        instance_segments(key)
    with pytest.raises(ValueError):
        instance_prefixes(key)


def test_store_visibility_and_shadowing() -> None:
    store = ValueStore()
    store.put(N1, "", {"root": True})
    nested = "e1#0/d7"
    assert store.get(N1, nested) == {"root": True}
    assert store.producer(N1, nested) == ""
    store.put(N1, "e1#0", {"root": False})
    assert store.get(N1, nested) == {"root": False}
    assert store.get(N1, "e1#0") == {"root": False}
    assert store.producer(N1, nested) == "e1#0"
    assert store.get(N1, "e1#1") == {"root": True}
    assert store.get(N1, "") == {"root": True}
    assert store.has(N1, "e1#0")
    assert not store.has(N1, nested)
    assert store.producer(N2, nested) is None
    with pytest.raises(KeyError) as missing:
        store.get(N2, nested)
    assert N2 in str(missing.value)
    assert nested in str(missing.value)
    store.drop(N1, "e1#0")
    assert store.get(N1, nested) == {"root": True}
    store.drop(N1, "never")


def test_store_items_for_nested_each() -> None:
    store = ValueStore()
    store.set_items(E1, "", [["a", "b"], ["c"]])
    first = child_instance("", each_segment(E1, 0))
    second = child_instance("", each_segment(E1, 1))
    store.set_items(E2, first, ["a", "b"])
    store.set_items(E2, second, ["c"])
    consumer = child_instance(second, each_segment(E2, 0))
    assert store.items(E1, "") == [["a", "b"], ["c"]]
    assert store.items(E2, first) == ["a", "b"]
    assert store.item(E1, consumer) == ["c"]
    assert store.item(E2, consumer) == "c"
    assert store.item(E2, child_instance(first, each_segment(E2, 1))) == "b"
    with pytest.raises(KeyError) as outside:
        store.item(E2, first)
    assert E2 in str(outside.value)
    assert first in str(outside.value)
    with pytest.raises(KeyError):
        store.items(E2, "")
    with pytest.raises(KeyError):
        store.item(E2, child_instance(first, each_segment(E2, 5)))


def test_store_is_thread_safe() -> None:
    store = ValueStore()
    barrier = threading.Barrier(8)

    def worker(index: int) -> None:
        barrier.wait()
        for value in range(200):
            store.put(f"n{index:010d}", f"e1#{value}", value)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    for index in range(8):
        for value in range(200):
            assert store.get(f"n{index:010d}", f"e1#{value}") == value
    assert len(store.snapshot(limit_bytes=10_000_000)) == 1600


def test_snapshot_order_keys_and_truncation() -> None:
    store = ValueStore()
    store.put(N2, "", "first")
    store.put(N1, "e1#0", [1, 2])
    store.put(N3, "", {"k": "v"})
    assert list(store.snapshot()) == [N2, f"{N1}@e1#0", N3]
    assert store.snapshot() == {N2: "first", f"{N1}@e1#0": [1, 2], N3: {"k": "v"}}
    big = ValueStore()
    for index in range(10):
        big.put(f"n{index:010d}", "", "x" * 40)
    limited = big.snapshot(limit_bytes=200)
    kept = [key for key in limited if key != "..."]
    assert limited["..."] == f"{10 - len(kept)} more"
    assert 0 < len(kept) < 10
    assert kept == [f"n{index:010d}" for index in range(len(kept))]
    without_marker = {key: value for key, value in limited.items() if key != "..."}
    assert len(json.dumps(without_marker, separators=(",", ":")).encode()) <= 200
    assert "..." not in big.snapshot()
    assert PAUSE_VALUES_BYTES == 65_536


def test_resolve_refs_paths_and_find() -> None:
    store = ValueStore()
    store.put(N1, "", {"items": [{"id": 7, "name": "Kadıköy"}, {"id": 9, "name": "Moda"}], "total": 2})
    store.put(N2, "", 9)
    assert resolve(ref(N1, "items", 0, "name"), store) == "Kadıköy"
    assert resolve(ref(N1, "items", -1, "id"), store) == 9
    find = ref(N1, "items", {"find": {"id": ref(N2)}}, "name")
    assert resolve(find, store) == "Moda"
    assert resolve(ref(N1, "items", {"filter": {"id": 7}}, {"len": True}), store) == 1
    nested = {"a": [ref(N2), {"b": ref(N1, "total")}], "c": "plain"}
    assert resolve(nested, store) == {"a": [9, {"b": 2}], "c": "plain"}
    assert FindStep.model_validate({"find": {"id": 1}}).find == {"id": 1}


def test_resolve_item_and_fmt() -> None:
    store = ValueStore()
    store.set_items(E1, "", [{"city": "Kadıköy", "n": 1}, {"city": "Moda", "n": 2}])
    store.put(N1, "", {"k": [1, None, True]})
    instance = child_instance("", each_segment(E1, 1))
    assert resolve({"$item": E1, "path": ["city"]}, store, instance) == "Moda"
    assert resolve({"$item": E1}, store, instance) == {"city": "Moda", "n": 2}
    fmt: dict[str, JsonValue] = {"$fmt": ["city=", {"$item": E1, "path": ["city"]}, " data=", ref(N1), " list=", ref(N1, "k"),
                    " n=", {"$item": E1, "path": ["n"]}, " flag=", ref(N1, "k", 2), " none=", ref(N1, "k", 1)]}
    assert resolve(fmt, store, instance) == (
        'city=Moda data={"k":[1,null,true]} list=[1,null,true] n=2 flag=true none=null'
    )
    store.put(N2, "", {"text": "Kadıköy"})
    assert resolve({"$fmt": [ref(N2)]}, store) == '{"text":"Kadıköy"}'
    assert fmt_text("plain") == "plain"
    assert fmt_text({"a": "ü"}) == '{"a":"ü"}'
    assert fmt_text(1.5) == "1.5"
    assert fmt_text(None) == "null"


def test_resolve_copies_containers() -> None:
    store = ValueStore()
    stored: dict[str, JsonValue] = {"list": [1, 2]}
    store.put(N1, "", stored)
    args: dict[str, JsonValue] = {"x": ref(N1), "y": [ref(N1, "list")], "z": {"keep": [1]}}
    before = json.dumps(args, sort_keys=True)
    result = resolve(args, store)
    assert json.dumps(args, sort_keys=True) == before
    assert isinstance(result, dict)
    x = result["x"]
    assert isinstance(x, dict)
    x_list = x["list"]
    assert isinstance(x_list, list)
    x_list.append(3)
    assert stored == {"list": [1, 2]}
    assert result["z"] is not args["z"]


@pytest.mark.parametrize(
    ("expr", "fragment"),
    [
        (ref(N2), "no value of node"),
        (ref(N1, "missing"), "missing key"),
        (ref(N1, "items", 5), "out of range"),
        (ref(N1, "items", {"find": {"id": 42}}), "no element matches"),
        ({"$item": E1}, "not inside an iteration"),
        ({"$ref": N1, "path": [{"bogus": 1}]}, "invalid RefExpr"),
    ],
)
def test_resolve_errors_name_node_and_expression(expr: dict[str, JsonValue], fragment: str) -> None:
    store = ValueStore()
    store.put(N1, "", {"items": [{"id": 1}]})
    with pytest.raises(RefResolutionError) as raised:
        resolve({"outer": [expr]}, store, "")
    error = raised.value
    assert error.code == "ref_unresolved"
    assert fragment in error.message
    target = expr.get("$ref", expr.get("$item"))
    assert isinstance(target, str)
    assert target in error.message
    assert json.dumps(expr, separators=(",", ":"), ensure_ascii=False) in error.message
    assert error.detail["expression"] == expr


def test_resolve_args_with_api_post(registry: Registry) -> None:
    spec = registry.op("api.post")
    store = ValueStore()
    store.put(N1, "", {"id": 12, "name": "Moda"})
    node = node_of("api.post", {"path": {"$fmt": ["/api/branches/", ref(N1, "id")]},
                                "json": {"name": ref(N1, "name"), "note": None}, "persona": None})
    resolved = resolve_args(node, spec, store)
    assert isinstance(resolved, ResolvedArgs)
    assert isinstance(resolved.model, Post)
    assert resolved.data == {"path": "/api/branches/12", "json": {"name": "Moda", "note": None}}
    assert "persona" not in resolved.data
    model = resolved.model
    assert isinstance(model, Post)
    assert model.path == "/api/branches/12"
    assert model.body == {"name": "Moda", "note": None}
    missing = node_of("api.post", {"path": ref(N2)})
    with pytest.raises(RefResolutionError) as raised:
        resolve_args(missing, spec, store)
    assert N3 in raised.value.message
    assert raised.value.detail["consumer"] == N3


def test_resolve_args_invalid(registry: Registry) -> None:
    spec = registry.op("api.post")
    store = ValueStore()
    store.put(N1, "", {"id": 12})
    node = node_of("api.post", {"path": ref(N1, "id"), "persona": "", "expect_status": [99], "bogus": 1,
                                "query": 3})
    with pytest.raises(NodeFailure) as raised:
        resolve_args(node, spec, store)
    error = raised.value
    assert error.code == "args_invalid"
    assert "api.post" in error.message
    errors = error.detail["errors"]
    assert isinstance(errors, list)
    assert len(errors) == 3
    count = error.detail["count"]
    assert isinstance(count, int)
    assert count > 3
    first = errors[0]
    assert isinstance(first, dict)
    assert set(first) == {"loc", "msg", "type"}


def test_validate_args_literal_json(registry: Registry) -> None:
    spec = registry.op("api.post")
    result = validate_args(spec, {"path": "/api/x", "json": [1, 2], "expect_status": [200, 201]})
    assert result.data == {"path": "/api/x", "json": [1, 2], "expect_status": [200, 201]}
    with pytest.raises(NodeFailure) as raised:
        validate_args(spec, {"json": {}})
    assert raised.value.code == "args_invalid"
    with pytest.raises(NodeFailure):
        validate_args(spec, [1, 2])


def test_validate_args_takes_resolved_values_at_ref_only_fields(registry: Registry) -> None:
    when = validate_args(registry.op("ctl.when"), {"cond": {"rows": []}})
    assert when.data == {"cond": {"rows": []}}
    each = validate_args(registry.op("ctl.each"), {"over": "not a list", "limit": 2})
    assert each.data == {"over": "not a list", "limit": 2}
    pause = validate_args(registry.op("debug.break"), {"label": "here", "when": 0})
    assert pause.data == {"label": "here", "when": 0}
    media = MediaInfo(uri="artifact:VID123", kind="video", width=1080, height=1920).model_dump(mode="json",
                                                                                          exclude_none=True)
    markers = validate_args(registry.op("media.markers"), {
        "media": media, "sources": [{"kind": "beat_tracker", "name": "song", "streams": ["beat"]}],
    })
    assert markers.data["media"] == media
    with pytest.raises(NodeFailure) as raised:
        validate_args(registry.op("media.markers"), {
            "media": {"uri": 1}, "sources": [{"kind": "beat_tracker", "name": "song", "streams": ["beat"]}],
        })
    assert raised.value.code == "args_invalid"
    lut = MediaInfo(uri="file:grade.cube", kind="file").model_dump(mode="json", exclude_none=True)
    render = validate_args(registry.op("edit.render"), {"timeline": {
        "fps": 30, "size": [1080, 1920], "tracks": [{"name": "main", "clips": [{
            "id": "main/0", "media": media, "start": "0s", "src": {"start": "0s", "end": "2s"},
            "effects": [{"kind": "color_grade", "lut": lut}, {"kind": "lut", "path": lut}],
        }]}],
    }})
    timeline = render.data["timeline"]
    assert isinstance(timeline, dict)
    tracks = timeline["tracks"]
    assert isinstance(tracks, list)
    track = tracks[0]
    assert isinstance(track, dict)
    clips = track["clips"]
    assert isinstance(clips, list)
    clip = clips[0]
    assert isinstance(clip, dict)
    assert clip["media"] == media
    effects = clip["effects"]
    assert isinstance(effects, list)
    first_effect = effects[0]
    assert isinstance(first_effect, dict)
    assert first_effect["lut"] == lut
    with pytest.raises(NodeFailure):
        validate_args(registry.op("ctl.when"), {"cond": ref(N1), "extra": 1})


def _group(name: str, *children: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return {"id": name, "kind": "group", "children": list(children)}


def _layer_media(layer: JsonValue, depth: int) -> JsonValue:
    current = layer
    for _ in range(depth):
        assert isinstance(current, dict)
        children = current["children"]
        assert isinstance(children, list)
        current = children[0]
    assert isinstance(current, dict)
    return current["media"]


@pytest.mark.parametrize("depth", [0, 1, 2, 3])
def test_resolve_args_inside_recursive_models(registry: Registry, depth: int) -> None:
    media = MediaInfo(uri="artifact:IMG123", kind="image", width=64, height=64).model_dump(mode="json",
                                                                                         exclude_none=True)
    store = ValueStore()
    store.put(N1, "", media)
    layer: dict[str, JsonValue] = {"id": "img", "kind": "image", "media": ref(N1)}
    for level in range(depth):
        layer = _group(f"g{level}", layer)
    spec = registry.op("gfx.render")
    node = node_of("gfx.render", {"scene": {"size": [64, 64], "layers": [layer]}}, out="media")
    resolved = resolve_args(node, spec, store)
    scene = resolved.data["scene"]
    assert isinstance(scene, dict)
    layers = scene["layers"]
    assert isinstance(layers, list)
    assert _layer_media(layers[0], depth) == media
    with pytest.raises(NodeFailure) as raised:
        validate_args(spec, {"scene": {"size": [64, 64], "layers": [layer]}})
    assert raised.value.code == "args_invalid"


def test_validate_args_keeps_recursive_models_without_ref_fields(registry: Registry) -> None:
    target: dict[str, JsonValue] = {"by": "role", "args": {"role": "main"}}
    for level in range(3):
        target = {"by": "test_id", "args": {"id": f"box{level}"}, "within": target}
    resolved = validate_args(registry.op("ui.tap"), {"target": target})
    assert resolved.data["target"] == target


def test_resolve_args_for_containers(registry: Registry) -> None:
    store = ValueStore()
    store.put(N1, "", {"rows": [1, 2]})
    when = resolve_args(node_of("ctl.when", {"cond": ref(N1, "rows")}), registry.op("ctl.when"), store)
    assert when.data == {"cond": [1, 2]}
    each = resolve_args(node_of("ctl.each", {"over": ref(N1, "rows")}), registry.op("ctl.each"), store)
    assert each.data["over"] == [1, 2]


def test_truthy() -> None:
    for falsy in (None, False, 0, 0.0, "", [], {}):
        assert truthy(falsy) is False
    truthful: list[JsonValue] = [1, "0", [0], {"a": None}, True, -1, 0.5, " "]
    for value in truthful:
        assert truthy(value) is True


def test_output_problem_for_every_type() -> None:
    assert output_problem("none", None) is None
    assert output_problem("none", 0) is not None
    plain: list[JsonValue] = [None, 1, 1.5, "x", [1, {"a": None}], {"a": [True]}]
    for value in plain:
        assert output_problem("json", value) is None
    assert output_problem("json", float("nan")) is not None
    assert output_problem("str", "x") is None
    assert output_problem("str", 1) == "expected str, got int"
    assert output_problem("int", 3) is None
    assert output_problem("int", True) == "expected int, got bool"
    assert output_problem("int", 3.0) == "expected int, got float"
    assert output_problem("float", 3) is None
    assert output_problem("float", 2.5) is None
    assert output_problem("float", False) == "expected float, got bool"
    nan = output_problem("float", float("nan"))
    assert nan is not None
    assert "nan" in nan
    assert output_problem("float", float("inf")) is not None
    assert output_problem("bool", True) is None
    assert output_problem("bool", 1) == "expected bool, got int"
    artifact = Artifact(id="a1", kind="screenshot", name="x.png", size=10).model_dump(mode="json")
    assert output_problem("artifact", artifact) is None
    invalid = output_problem("artifact", {"id": "a1"})
    assert invalid is not None
    assert "Artifact" in invalid
    assert output_problem("artifact", "a1") == "expected artifact, got str"
    media = MediaInfo(uri="file:a.mp4", kind="video", width=10, height=20).model_dump(mode="json")
    assert output_problem("media", media) is None
    assert output_problem("media", {"uri": "x", "kind": "hologram"}) is not None
    assert output_problem("list[int]", [1, 2, 3]) is None
    bad_list = output_problem("list[int]", [1, 2, "3", 4])
    assert bad_list == "expected list[int], item 2 is not int: got str"
    assert output_problem("list[int]", {"a": 1}) == "expected list[int], got object"
    assert output_problem("list[list[str]]", [["a"], ["b", 1]]) is not None
    assert output_problem("resource:tenant", {"id": "t1"}) is None
    assert output_problem("resource:tenant", ["t1"]) == "expected resource:tenant, got list"
    unknown = output_problem("hologram", 1)
    assert unknown is not None
    assert "hologram" in unknown


def test_checked_output() -> None:
    node = Node(id=N1, op="api.get", out="int")
    assert checked_output(node, 5) == 5
    value: dict[str, JsonValue] = {"a": 1}
    assert checked_output(Node(id=N1, op="api.get", out="json"), value) is value
    with pytest.raises(OutputTypeError) as raised:
        checked_output(node, "5")
    error = raised.value
    assert error.code == "output_type"
    assert error.detail == {"out": "int", "produced": "str"}
    assert N1 in error.message
    assert "expected int, got str" in error.message


def test_value_digest() -> None:
    a = {"b": [1, {"y": 2, "x": "Kadıköy"}], "a": None}
    b = {"a": None, "b": [1, {"x": "Kadıköy", "y": 2}]}
    assert value_digest(a) == value_digest(b)
    assert value_digest(a) == digest(canonical_json(a))
    assert value_digest(a).startswith("sha256:")
    assert value_digest([1, 2]) != value_digest([2, 1])
