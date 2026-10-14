from __future__ import annotations

from pathlib import Path

import pytest

from eks.discovery import AppInfo, DiscoveryError, find_app, find_tree


def write_app(root: Path, body: str) -> Path:
    harness = root / ".harness"
    harness.mkdir(parents=True, exist_ok=True)
    (harness / "app.toml").write_text(body, encoding="utf-8")
    return root


def make_tree(tree: Path) -> Path:
    (tree / ".harness").mkdir(parents=True)
    (tree / ".harness" / "project.toml").write_text('[project]\nid = "acme/shop"\n', encoding="utf-8")
    write_app(tree / "src" / "web-app", '[app]\nname = "storefront"\nplatforms = ["web"]\n')
    write_app(tree / "src" / "mobile-app", '[app]\nplatforms = ["ios", "android"]\n')
    write_app(tree / "src" / "kiosk", "[mobile]\nbundle = 'x'\n")
    write_app(tree / "node_modules" / "dep", '[app]\nname = "dep-app"\n')
    write_app(tree / ".claude" / "worktrees" / "wt" / "web-app", '[app]\nname = "storefront"\n')
    return tree


def test_find_app_by_app_name(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "shop")
    app = find_app(tree, "storefront")
    assert app == AppInfo(name="storefront", root=(tree / "src" / "web-app").resolve(), platforms=("web",))


def test_find_app_by_directory_name(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "shop")
    mobile = find_app(tree, "mobile-app")
    assert mobile is not None
    assert mobile.name == "mobile-app"
    assert mobile.platforms == ("ios", "android")
    web = find_app(tree, "web-app")
    assert web is not None
    assert web.name == "storefront"


def test_platforms_default_from_the_profile(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "shop")
    kiosk = find_app(tree, "kiosk")
    assert kiosk is not None
    assert kiosk.platforms == ("ios", "android")


def test_missing_and_skipped_apps(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "shop")
    assert find_app(tree, "nothing") is None
    assert find_app(tree, "dep-app") is None
    assert find_app(tmp_path / "empty", "web-app") is None


def test_app_name_wins_over_directory_name(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "shop")
    write_app(tree / "src" / "storefront", '[app]\nname = "legacy"\n')
    app = find_app(tree, "storefront")
    assert app is not None
    assert app.root == (tree / "src" / "web-app").resolve()


def test_app_name_wins_over_an_unnamed_app_in_a_matching_directory(tmp_path: Path) -> None:
    # src/storefront declares no [app] name; its directory name must not compete with web-app's declared name.
    tree = make_tree(tmp_path / "shop")
    write_app(tree / "src" / "storefront", '[app]\nplatforms = ["ios"]\n')
    app = find_app(tree, "storefront")
    assert app == AppInfo(name="storefront", root=(tree / "src" / "web-app").resolve(), platforms=("web",))


def test_unnamed_apps_sharing_a_directory_name_raise(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "shop")
    write_app(tree / "other" / "mobile-app", '[app]\nplatforms = ["android"]\n')
    with pytest.raises(DiscoveryError, match="several apps are named 'mobile-app'"):
        find_app(tree, "mobile-app")


def test_duplicate_app_names_raise(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "shop")
    write_app(tree / "src" / "web-app-2", '[app]\nname = "storefront"\n')
    with pytest.raises(DiscoveryError, match="several apps are named 'storefront'"):
        find_app(tree, "storefront")


def test_a_broken_profile_only_matters_for_its_own_app(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "shop")
    write_app(tree / "src" / "broken", "[app\n")
    assert find_app(tree, "storefront") is not None
    with pytest.raises(DiscoveryError, match="app 'broken'"):
        find_app(tree, "broken")


def test_an_extending_app_keeps_its_own_name_and_root(tmp_path: Path) -> None:
    # marketing extends mobile-app without a name of its own: it must not inherit 'mobile-app'.
    tree = make_tree(tmp_path / "shop")
    write_app(tree / "marketing", '[app]\nextends = "../src/mobile-app"\n')
    write_app(tree / "promo", '[app]\nextends = "../src/web-app"\nname = "promo-site"\n')
    mobile = find_app(tree, "mobile-app")
    assert mobile == AppInfo(name="mobile-app", root=(tree / "src" / "mobile-app").resolve(),
                             platforms=("ios", "android"))
    marketing = find_app(tree, "marketing")
    assert marketing == AppInfo(name="marketing", root=(tree / "marketing").resolve(), platforms=("ios", "android"))
    promo = find_app(tree, "promo-site")
    assert promo == AppInfo(name="promo-site", root=(tree / "promo").resolve(), platforms=("web",))
    storefront = find_app(tree, "storefront")
    assert storefront is not None
    assert storefront.root == (tree / "src" / "web-app").resolve()


def test_root_app(tmp_path: Path) -> None:
    tree = write_app(tmp_path / "solo", '[app]\nname = "solo-app"\nplatform = "android"\n')
    app = find_app(tree, "solo-app")
    assert app == AppInfo(name="solo-app", root=tree.resolve(), platforms=("android",))


def test_find_tree_uses_project_toml(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "shop")
    nested = tree / "src" / "web-app" / "deep"
    nested.mkdir(parents=True)
    assert find_tree(nested) == tree.resolve()
