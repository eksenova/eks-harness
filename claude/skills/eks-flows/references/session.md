# Session builders

Signatures as they are in `src/eks/*.py`. `TargetLike` is a `Target` or a shorthand string, `Text` a string
(f-strings may carry Refs), every duration a `Time`. `at=` is a pin and is accepted only inside a score take.
Platforms come from the op registry: `docs/reference/ops.md` lists each op's platforms and arguments.

## s.ui (web, ios, android unless noted)

```text
nav(route, params=None, *, timeout=None)              web: path plus query params; React Native: route name and params
back(*, timeout=None); reload(*, timeout=None)
open_url(url, *, timeout=None); deeplink(url, *, timeout=None)        deeplink: ios, android
tap(target, *, force=False, real=None, timeout=None, settle=True)
double_tap(target, ...); hover(target, ...)                           hover: web
long_press(target, *, hold=None, ...)
fill(target, value, *, submit=False, ...); type(target, value, *, pace=None, submit=False, ...)
clear(target, ...); key(key, target=None, ...)
select(target, value | [values], ...); check(target, on=True, ...); choose(trigger, option, ...)
scroll(target=None, *, to="top"|"bottom"|"start"|"end"|target, by=(dx, dy), ...)
swipe("up"|"down"|"left"|"right", *, target=None, distance=None, ...)
drag(source, to, ...)
text(target=None) -> Ref[str]; value(target) -> Ref[str]; attr(target, name) -> Ref[Json] (web)
count(target) -> Ref[int]; exists(target) -> Ref[bool]; visible(target) -> Ref[bool]
tree(target=None, *, max_lines=None) -> Ref[str]
url() -> Ref[str] (web); route() -> Ref[Json]; state(path=None) -> Ref[Json]; layout(target) -> Ref[Json]
wait_for(target, *, state="visible"|"attached"|"hidden"|"enabled", timeout=None)
wait_gone(target, *, timeout=None); wait_url(pattern, *, timeout=None) (web)
settle(*, strict=True, timeout=None)
```

Reads take `timeout=` to override the session default for that read. `ui.state` on web reads
`window.__ehxState(path)`; without it the op answers `op_unsupported`.

## expect

```text
expect(target, timeout=None).visible() | hidden() | enabled() | disabled() | checked(on=True)
expect(target).text(equals=None, *, contains=None, matches=None)
expect(target).value(...); expect(target).attr(name, equals=None, *, contains=None, matches=None)
expect(target).count(equals=None, *, at_least=None, at_most=None)
expect(ref).eq(v) | ne(v) | contains(v) | matches(pattern) | gt(v) | ge(v) | lt(v) | le(v) | truthy() | falsy()
expect(ref).length(equals=None, *, at_least=None, at_most=None)
expect.soft(subject, timeout=None)        record a failure without failing the run
expect.eventually(subject, timeout=None)  retry until the timeout
```

Every assertion takes `message=`. A value is checked once; waiting on a value is `expect.eventually(ref)`,
which re-runs the producing read.

## Evidence

```text
s.checkpoint(label, *, viewport=None, checks=None, shot=True, full=False, soft=False) -> Ref[Json]
s.shot(name, *, element=None, focus=None, full=False, viewport=None, hide=()) -> Ref[Artifact]
s.dump("dom"|"a11y"|"mhtml", *, name=None, viewport=None) -> Ref[Artifact]      web
s.har(*, name=None, whole=False) -> Ref[Artifact]                                web
with s.record(name, *, pace=None, viewport=None, pointer=True, from_here=True, focus=None, trim=None, hide=()) as video:
s.hold(duration, *, reason=None)                                                 only inside a recording
eks.annotate(artifact, overlays, *, style=None) -> Ref[Artifact]
```

## s.overlay (live) and eks.overlay (values)

```text
title(text, *, subtitle=None, kicker=None, hold=None, keep=False, key=None, style=None, emphasis="none")
caption(text, *, position="bottom", step=None, hold=None, keep=False, key=None, placement=None, style=None)
callout(anchor, text, *, hold=None, keep=False, key=None, placement=None, style=None)
spotlight(anchor, text=None, *, dim=None, ...); highlight(anchor, *, shape="box"|"outline"|"rounded", ...)
arrow(anchor, to, text=None, ...); blur(anchor, *, radius=None, ...); redact(anchor, ...)
icon(anchor, name, ...); image(anchor, media, *, width=None, ...); step(anchor, index=None, ...)
s.overlay.show(model); s.overlay.remove(key); s.overlay.clear(kind=None)
```

`eks.overlay.<kind>(..., at=<Span>)` builds the same overlay as a value for `eks.annotate` and for edit
tracks. Styles: `review` (red), `kb` (gold), or plugin presets.

## Backend, fakes, mobile, web

```text
s.api.get(path, *, query=None, headers=None, as_=None, expect_status=None) -> Ref[Json]
s.api.post|put|patch(path, *, json=None, query=None, headers=None, as_=None, expect_status=None)
s.api.delete(path, ...)
s.as_(persona); s.backend.info -> Ref[Json]
s.fake.arm(slot, *, mode="success"|"error"|"cancel", media=None, fixture=None, data=None); s.fake.clear(slot=None)
s.mobile.dispatch(action) -> Ref[Json]; s.mobile.state(path=None)
s.mobile.patch(target, *, props=None, style=None, text=None, key=None); s.mobile.unpatch(key)
s.mobile.mount(component, *, props=None, target=None, key=None); s.mobile.unmount(key)
s.mobile.invoke(target, prop, *args) -> Ref[Json]; s.mobile.toggle(target, value=None); s.mobile.submit(target)
s.mobile.idle(*, timeout=None); s.mobile.route_names(); s.mobile.logout(); s.mobile.close_dev_menu()
s.mobile.pointer(target=None, *, duration=None)
s.web.wheel(target=None, *, dx=0, dy=400, real=None); s.web.viewport(name)
s.web.logs(kind="console", *, limit=100) -> Ref[Json]; s.web.clear_logs()
s.eval(source, arg=None) -> Ref[Json]
```

`media=` of `s.fake.arm` is a media ref: `eks.media.source("fixtures/id-front.jpg")`. `fixture=` names a file
in the app's fixture folders.
