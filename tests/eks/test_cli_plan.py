from __future__ import annotations

import json
import re
import shutil
import sys
import textwrap
from pathlib import Path

import pytest

import eks
from eks.discovery import tree_registry
from eks.tracing import TraceError
from eks.workspace import plan_spec
from eks_harness.cli import main
from eks_harness.cli.plan_cmds import PLAN_INVALID, Entry, _source_line
from eks_harness.cli.ui import EXIT_USAGE
from eks_harness.ir.registry import Registry

FIXTURES = Path(__file__).parent / "cli_fixtures"
HEADER = re.compile(r"^(?P<kind>\w+) (?P<name>[\w-]+): (?P<nodes>\d+) nodes?, (?P<resources>\d+) resources?, "
                    r"hash (?P<hash>[0-9a-f]{16})$", re.MULTILINE)

PLUGIN_MANIFEST = """
    [plugin]
    id = "acme"
    name = "Acme"
    version = "1.2.0"
    api = "1"
    modules = ["acme_ops"]
"""

PLUGIN_OPS = """
    import eks


    @eks.op("acme", out="json", resources=("lease",), description="Ping the acme widget")
    class Ping(eks.Args):
        target: str = "self"
"""

PLUGIN_FLOW = """
    import eks


    @eks.flow(app="web-app")
    def pinger(s: eks.Session):
        s.op("acme.ping")
"""


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "tree"
    shutil.copytree(FIXTURES, root, ignore=shutil.ignore_patterns("__pycache__"))
    monkeypatch.chdir(root)
    monkeypatch.setenv("EKS_HARNESS_NO_PLUGIN_CLI", "1")
    return root


def write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    return path


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(["plan", *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def run_json(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict]:
    code, out, _err = run(capsys, *argv, "--json")
    return code, json.loads(out)


def header(out: str) -> re.Match[str]:
    found = HEADER.search(out)
    assert found is not None, out
    return found


def test_text_output_has_header_table_and_no_issues(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = run(capsys, ".harness/flows/subeler.py")
    assert code == 0, err
    plan = plan_spec(".harness/flows/subeler.py")
    walked = [node for node, _ in plan.walk()]
    found = header(out)
    assert (found["kind"], found["name"]) == ("flow", "subeler")
    assert int(found["nodes"]) == len(walked)
    assert int(found["resources"]) == len(plan.resources) == 2
    assert plan.hash.removeprefix("sha256:").startswith(found["hash"])
    # the first line is the header, the table lists every node in walk order
    assert out.splitlines()[0] == found.group(0)
    positions = [out.index(node.id) for node in walked]
    assert positions == sorted(positions)
    for column in ("id", "op", "args", "uses", "after", "src"):
        assert column in out
    for op in ("session.as", "api.get", "ui.nav", "check.checkpoint"):
        assert op in out
    assert ".harness/flows/subeler.py:" in out
    assert "valid: no issues" in out


def test_nested_nodes_are_indented_by_depth_with_their_branch(
    tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = run(capsys, ".harness/flows/kasa.py::kasa")
    assert code == 0, err
    plan = plan_spec(".harness/flows/kasa.py::kasa")
    when = next(node for node in plan.nodes if node.op == "ctl.when")
    assert when.branches is not None
    taps = [*when.branches["then"], *when.branches["else"]]
    assert re.search(rf"^\s+{when.id}\s+ctl\.when\s", out, re.MULTILINE)
    for tap, branch in zip(taps, ("then", "else"), strict=True):
        line = next(line for line in out.splitlines() if tap.id in line)
        assert f"  {tap.id}" in line
        assert f"ui.tap [{branch}]" in line
    top = next(line for line in out.splitlines() if when.id in line)
    tapped = next(line for line in out.splitlines() if taps[0].id in line)
    assert tapped.index(taps[0].id) - top.index(when.id) == 2


def test_args_summary_is_compact_json_cut_at_120_characters(
    tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = run(capsys, ".harness/edits/demo.py")
    assert code == 0, err
    assert header(out)["kind"] == "edit"
    assert '{"uri": "artifact:VID123"}' in out
    long_rows = [line for line in out.splitlines() if "edit.render" in line]
    assert len(long_rows) == 1
    summary = re.search(r'(\{"timeline".*?\.\.\.)', long_rows[0])
    assert summary is not None
    assert len(summary.group(1)) == 120


def test_json_output_is_the_plan_and_the_report(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, data = run_json(capsys, ".harness/flows/subeler.py")
    assert code == 0
    assert set(data) == {"plan", "validation"}
    assert data["plan"]["schema"] == "eks/plan@1"
    assert data["plan"]["kind"] == "flow"
    assert data["plan"]["name"] == "subeler"
    assert data["validation"]["ok"] is True
    assert data["validation"]["issues"] == []
    plan = plan_spec(".harness/flows/subeler.py")
    assert data["plan"] == json.loads(plan.to_json())
    assert data["validation"] == eks.validate(plan, tree=tree).as_dict()
    assert data["validation"]["hash"] == plan.hash


def test_global_json_flag_works_too(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["--json", "plan", ".harness/flows/subeler.py"])
    data = json.loads(capsys.readouterr().out)
    assert code == 0
    assert data["validation"]["ok"] is True


def test_json_output_is_deterministic(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    first = run(capsys, ".harness/flows/subeler.py", "--json")
    second = run(capsys, ".harness/flows/subeler.py", "--json")
    assert first[0] == second[0] == 0
    assert first[1] == second[1]


def test_text_output_is_deterministic(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    first = run(capsys, ".harness/flows/kasa.py::iade")
    second = run(capsys, ".harness/flows/kasa.py::iade")
    assert first[0] == 0
    assert first[1] == second[1]


def test_name_selects_one_entry_of_a_file(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, data = run_json(capsys, ".harness/flows/kasa.py::iade")
    assert code == 0
    assert data["plan"]["name"] == "iade"
    assert "ctl.each" in [node["op"] for node in data["plan"]["nodes"]]
    code, data = run_json(capsys, ".harness/flows/kasa.py::kasa")
    assert code == 0
    assert data["plan"]["name"] == "kasa"
    assert data["plan"]["session"]["persona"] == "harman-kasiyer"


def test_a_file_with_several_entries_needs_a_name(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = run(capsys, ".harness/flows/kasa.py")
    assert code == EXIT_USAGE
    assert out == ""
    assert "kasa.py::iade" in err
    assert "kasa.py::kasa" in err
    assert "defines several entries (iade (flow), kasa (flow))" in err


def test_set_parameters_are_coerced_into_their_types(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, data = run_json(capsys, ".harness/flows/subeler.py", "--set", "rows=3", "--set", "branch=Moda")
    assert code == 0
    assert data["plan"]["params"] == {"branch": "Moda", "rows": 3}
    nav = [node for node in data["plan"]["nodes"] if node["op"] == "ui.nav"]
    assert len(nav) == 2
    route = nav[1]["args"]["route"]["$fmt"]
    assert route[1]["path"] == ["items", {"find": {"name": "Moda"}}, "id"]
    default = run_json(capsys, ".harness/flows/subeler.py")[1]
    assert default["plan"]["params"] == {"branch": "Kadıköy", "rows": 1}
    assert default["validation"]["hash"] != data["validation"]["hash"]


@pytest.mark.parametrize("argv", [
    ("--set", "rows=many"),
    ("--set", "rows"),
    ("--set", "rows=1", "--set", "rows=2"),
    ("--set", "unknown=1"),
])
def test_bad_set_values_are_usage_errors(tree: Path, capsys: pytest.CaptureFixture[str], argv: tuple[str, ...]) -> None:
    code, out, err = run(capsys, ".harness/flows/subeler.py", *argv)
    assert code == EXIT_USAGE
    assert out == ""
    assert err.strip()


def test_platform_picks_the_session_platform(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    for platform in ("ios", "android"):
        code, data = run_json(capsys, "mobile-app/.harness/flows/tapper.py", "--platform", platform)
        assert code == 0
        assert data["plan"]["session"]["platform"] == platform
        leases = [resource["kind"] for resource in data["plan"]["resources"] if resource["kind"].startswith("lease.")]
        assert leases == [f"lease.{platform}"]


def test_a_flow_on_several_platforms_needs_platform(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = run(capsys, "mobile-app/.harness/flows/tapper.py")
    assert code == EXIT_USAGE
    assert out == ""
    assert "--platform" in err


def test_a_platform_outside_the_flow_is_a_usage_error(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, _out, err = run(capsys, ".harness/flows/subeler.py", "--platform", "ios")
    assert code == EXIT_USAGE
    assert "ios" in err


def test_an_invalid_flow_exits_1_with_the_issue(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = run(capsys, ".harness/flows/broken.py")
    assert code == PLAN_INVALID == 1
    assert header(out)["name"] == "broken"
    assert "ERROR persona.unknown" in out
    assert "nobody" in out
    assert "1 validation error" in err


def test_an_invalid_flow_in_json_reports_the_issue(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, data = run_json(capsys, ".harness/flows/broken.py")
    assert code == PLAN_INVALID
    assert data["validation"]["ok"] is False
    assert [issue["code"] for issue in data["validation"]["issues"]] == ["persona.unknown"]
    assert data["plan"]["name"] == "broken"


def test_a_missing_file_exits_2(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = run(capsys, ".harness/flows/missing.py")
    assert code == EXIT_USAGE
    assert out == ""
    assert "missing.py" in err


def test_a_missing_file_in_json_mode_prints_the_error_as_json(
    tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _err = run(capsys, ".harness/flows/missing.py", "--json")
    assert code == EXIT_USAGE
    data = json.loads(out)
    assert data["exitCode"] == EXIT_USAGE
    assert data["error"] == "usage"
    assert "missing.py" in data["message"]


def test_an_unknown_name_exits_2_with_suggestions(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = run(capsys, ".harness/flows/kasa.py::kasaa")
    assert code == EXIT_USAGE
    assert out == ""
    assert "'kasaa'" in err
    assert "did you mean 'kasa'" in err


def test_a_trace_error_exits_2_with_the_user_source_line(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = run(capsys, ".harness/flows/raises.py")
    assert code == EXIT_USAGE
    assert out == ""
    assert "eks.parallel() cannot be nested" in err
    assert ".harness/flows/raises.py:8: with eks.parallel():" in err


def test_a_validation_error_while_tracing_exits_2_with_the_source_line(
    tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(tree / ".harness" / "flows" / "bad_args.py", """
        import eks


        @eks.flow(app="web-app")
        def bad_args(s: eks.Session):
            s.ui.nav("/a")
            s.record_op("ui.tap", {"target": "#save", "force": "sometimes"})
    """)
    code, _out, err = run(capsys, ".harness/flows/bad_args.py")
    assert code == EXIT_USAGE
    assert "force" in err
    assert ".harness/flows/bad_args.py:7:" in err


def test_a_time_error_while_tracing_exits_2(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write(tree / ".harness" / "flows" / "bad_time.py", """
        import eks
        from eks import t


        @eks.flow(app="web-app")
        def bad_time(s: eks.Session):
            s.ui.wait_for("#row", timeout=t("soon"))
    """)
    code, _out, err = run(capsys, ".harness/flows/bad_time.py")
    assert code == EXIT_USAGE
    assert "soon" in err
    assert ".harness/flows/bad_time.py:7:" in err


def test_a_target_error_while_tracing_exits_2(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write(tree / ".harness" / "flows" / "bad_target.py", """
        import eks


        @eks.flow(app="web-app")
        def bad_target(s: eks.Session):
            s.ui.tap("role=")
    """)
    code, _out, err = run(capsys, ".harness/flows/bad_target.py")
    assert code == EXIT_USAGE
    assert "role=" in err
    assert ".harness/flows/bad_target.py:6:" in err


def test_a_validation_error_shows_the_offending_value(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write(tree / ".harness" / "flows" / "bad_value.py", """
        import eks


        @eks.flow(app="web-app")
        def bad_value(s: eks.Session):
            s.record_op("ui.tap", {"target": "#save", "force": "sometimes"})
    """)
    code, _out, err = run(capsys, ".harness/flows/bad_value.py")
    assert code == EXIT_USAGE
    assert "input_value='sometimes'" in err
    assert ".harness/flows/bad_value.py:6:" in err


def test_markup_like_text_in_an_error_is_printed_verbatim(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write(tree / ".harness" / "flows" / "markup_time.py", """
        import eks
        from eks import t


        @eks.flow(app="web-app")
        def markup_time(s: eks.Session):
            s.ui.wait_for("#row", timeout=t("[/x]"))
    """)
    code, out, err = run(capsys, ".harness/flows/markup_time.py")
    assert code == EXIT_USAGE
    assert out == ""
    assert "[/x]" in err
    assert ".harness/flows/markup_time.py:7:" in err
    code, out = run_json(capsys, ".harness/flows/markup_time.py")
    assert code == EXIT_USAGE
    assert "[/x]" in out["message"]
    assert "\\[" not in out["message"]


def test_a_mistyped_session_member_exits_2_with_the_source_line(
    tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(tree / ".harness" / "flows" / "typo.py", """
        import eks


        @eks.flow(app="web-app")
        def typo(s: eks.Session):
            s.ui.nav("/a")
            s.ui.tapp("#save")
    """)
    code, out, err = run(capsys, ".harness/flows/typo.py")
    assert code == EXIT_USAGE
    assert out == ""
    assert "AttributeError" in err
    assert "tapp" in err
    assert '.harness/flows/typo.py:7: s.ui.tapp("#save")' in err


def test_a_route_patch_with_none_exits_2_with_the_source_line(
    tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(tree / ".harness" / "flows" / "patch_none.py", """
        import eks


        @eks.flow(app="web-app")
        def patch_none(s: eks.Session):
            s.net.route("GET", "/api/rows").patch(lambda_path="$.items", value=None)
            s.ui.nav("/a")
    """)
    code, _out, err = run(capsys, ".harness/flows/patch_none.py")
    assert code == EXIT_USAGE
    assert "TypeError" in err
    assert ".harness/flows/patch_none.py:6:" in err


def test_a_name_error_in_a_flow_body_exits_2_with_the_source_line(
    tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(tree / ".harness" / "flows" / "undefined.py", """
        import eks


        @eks.flow(app="web-app")
        def undefined(s: eks.Session):
            s.ui.nav("/a")
            s.ui.tap(undefined_name)
    """)
    code, _out, err = run(capsys, ".harness/flows/undefined.py")
    assert code == EXIT_USAGE
    assert "NameError" in err
    assert ".harness/flows/undefined.py:7: s.ui.tap(undefined_name)" in err


def test_a_module_level_exception_exits_2_with_the_source_line(
    tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(tree / ".harness" / "flows" / "divide.py", """
        import eks

        ratio = 1 / 0


        @eks.flow(app="web-app")
        def divide(s: eks.Session):
            s.ui.nav("/a")
    """)
    code, _out, err = run(capsys, ".harness/flows/divide.py")
    assert code == EXIT_USAGE
    assert "ZeroDivisionError" in err
    assert ".harness/flows/divide.py:3: ratio = 1 / 0" in err


def test_a_helper_error_names_the_helper_and_the_entry_line(
    tree: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # a tree helper module imported by the flow: the innermost user frame is the helper,
    # and the entry file's calling line is shown as well
    monkeypatch.syspath_prepend(str(tree / ".harness" / "flows"))
    monkeypatch.delitem(sys.modules, "helpers_lib", raising=False)
    write(tree / ".harness" / "flows" / "helpers_lib.py", """
        def broken(s):
            return s.ui.tapp("#save")
    """)
    write(tree / ".harness" / "flows" / "uses_helper.py", """
        import eks

        from helpers_lib import broken


        @eks.flow(app="web-app")
        def uses_helper(s: eks.Session):
            s.ui.nav("/a")
            broken(s)
    """)
    try:
        code, _out, err = run(capsys, ".harness/flows/uses_helper.py")
    finally:
        sys.modules.pop("helpers_lib", None)
    assert code == EXIT_USAGE, err
    assert '.harness/flows/helpers_lib.py:2: return s.ui.tapp("#save")' in err
    assert "called from .harness/flows/uses_helper.py:9: broken(s)" in err


def test_the_source_line_skips_library_frames_inside_the_tree(
    tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import eks_harness

    library_parent = Path(eks.__file__).resolve().parent.parent
    assert Path(eks_harness.__file__).resolve().parent.parent == library_parent
    flow = (tree / ".harness" / "flows" / "raises.py").resolve()
    with pytest.raises(TraceError) as caught:
        plan_spec(str(flow))
    entry = Entry(flow, (flow.parent, library_parent, Path(sys.prefix).resolve().parent))
    where = _source_line(caught.value, entry, tree.resolve())
    assert where == ".harness/flows/raises.py:8: with eks.parallel():"


def test_an_internal_error_without_a_user_frame_stays_unexpected(
    tree: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import eks.workspace

    def explode(*_args: object, **_kwargs: object) -> None:
        raise KeyError("internal")

    monkeypatch.setattr(eks.workspace, "plan_spec", explode)
    code, _out, err = run(capsys, ".harness/flows/subeler.py")
    assert code == 1
    assert "KeyError" in err
    assert " at " not in err


def test_a_syntax_error_exits_2_with_its_line(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write(tree / ".harness" / "flows" / "syntax.py", """
        import eks


        @eks.flow(app="web-app")
        def syntax(s: eks.Session):
            s.ui.nav("/a"
    """)
    code, _out, err = run(capsys, ".harness/flows/syntax.py")
    assert code == EXIT_USAGE
    assert "SyntaxError" in err
    assert ".harness/flows/syntax.py:6: s.ui.nav(\"/a\"" in err


def test_seed_plans_with_its_personas(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = run(capsys, ".harness/seeds/standard.py")
    assert code == 0, err
    assert header(out)["kind"] == "seed"
    code, data = run_json(capsys, ".harness/seeds/standard.py")
    assert code == 0
    assert data["plan"]["personas"] == ["harman-admin", "harman-kasiyer"]
    assert data["validation"]["ok"] is True


def test_edit_plans(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, data = run_json(capsys, ".harness/edits/demo.py")
    assert code == 0
    assert data["plan"]["kind"] == "edit"
    ops = [node["op"] for node in data["plan"]["nodes"]]
    assert "edit.render" in ops
    assert "check.media" in ops
    assert data["validation"]["ok"] is True


def test_scene_plans_with_its_media_parameter(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = run(capsys, ".harness/scenes/phone.py", "--set", "screen=artifact:S1")
    assert code == 0, err
    assert header(out)["kind"] == "scene"
    code, data = run_json(capsys, ".harness/scenes/phone.py", "--set", "screen=artifact:S1")
    assert code == 0
    ops = [node["op"] for node in data["plan"]["nodes"]]
    assert ops == ["media.source", "media.source", "blender.render"]
    assert data["plan"]["nodes"][0]["args"]["uri"] == "artifact:S1"


def test_scene_without_its_media_parameter_is_a_usage_error(
    tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = run(capsys, ".harness/scenes/phone.py")
    assert code == EXIT_USAGE
    assert out == ""
    assert "screen" in err


def test_plugins_load_unless_no_plugins(
    tree: Path, capsys: pytest.CaptureFixture[str], eks_registry: Registry
) -> None:
    write(tree / ".harness" / "project.toml", '[project]\nid = "acme/cli"\n\n[plugins]\ntrust = ["acme"]\n')
    plugin = tree / ".harness" / "plugins" / "acme"
    write(plugin / "harness-plugin.toml", PLUGIN_MANIFEST)
    write(plugin / "acme_ops.py", PLUGIN_OPS)
    write(tree / ".harness" / "flows" / "pinger.py", PLUGIN_FLOW)
    code, out, err = run(capsys, ".harness/flows/pinger.py", "--no-plugins")
    assert code == EXIT_USAGE
    assert out == ""
    assert "acme.ping" in err
    assert ".harness/flows/pinger.py:6:" in err
    code, data = run_json(capsys, ".harness/flows/pinger.py")
    assert code == 0
    assert [node["op"] for node in data["plan"]["nodes"]] == ["acme.ping"]
    assert data["plan"]["requires"] == [{"plugin": "acme", "version": "1.2.0"}]
    assert tree_registry(tree).has_op("acme.ping")
    assert not tree_registry(tree, plugins=False).has_op("acme.ping")
    assert not eks_registry.has_op("acme.ping")


def test_an_absolute_spec_works_from_another_directory(
    tree: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    code, data = run_json(capsys, f"{tree / '.harness' / 'flows' / 'kasa.py'}::kasa")
    assert code == 0
    assert data["plan"]["name"] == "kasa"
    assert data["validation"]["ok"] is True
