export type Json = null | boolean | number | string | Json[] | { [key: string]: Json }

export type Abort = { "kind"?: "abort"; "reason"?: "failed" | "aborted" | "timedout" | "connectionrefused" | "internetdisconnected" }

export type Arm = { "slot": string; "mode"?: "success" | "error" | "cancel"; "media"?: { "path": string; "mime": string; "name"?: string | null } | null; "fixture"?: string | null; "data"?: Json }

export type Arrow = { "kind"?: "arrow"; "key"?: string | null; "window"?: Span | null; "hold"?: number | null; "keep"?: boolean; "placement"?: Placement | null; "style"?: { [key: string]: Json } | null; "emphasis"?: "none" | "freeze" | "slow"; "anchor": Target; "to": Target; "text"?: string | null }

export type AsPersona = { "persona": string }

export type Back = { "timeout"?: number | null }

export type Blur = { "kind"?: "blur"; "key"?: string | null; "window"?: Span | null; "hold"?: number | null; "keep"?: boolean; "placement"?: Placement | null; "style"?: { [key: string]: Json } | null; "emphasis"?: "none" | "freeze" | "slow"; "anchor": Target; "radius"?: number | null }

export type Callout = { "kind"?: "callout"; "key"?: string | null; "window"?: Span | null; "hold"?: number | null; "keep"?: boolean; "placement"?: Placement | null; "style"?: { [key: string]: Json } | null; "emphasis"?: "none" | "freeze" | "slow"; "anchor": Target; "text": string }

export type Caption = { "kind"?: "caption"; "key"?: string | null; "window"?: Span | null; "hold"?: number | null; "keep"?: boolean; "placement"?: Placement | null; "style"?: { [key: string]: Json } | null; "emphasis"?: "none" | "freeze" | "slow"; "text": string; "position"?: "top" | "bottom"; "step"?: number | null }

export type Cassette = { "name": string; "mode"?: "record" | "replay" | "auto" }

export type Check = { "timeout"?: number | null; "real"?: boolean | null; "settle"?: boolean; "target": Target; "on"?: boolean }

export type CheckUse = { "kind": string; "params"?: { [key: string]: Json } }

export type Checkpoint = { "label": string; "viewport"?: Viewport | null; "checks"?: Array<CheckUse> | null; "shot"?: boolean; "full"?: boolean; "soft"?: boolean }

export type Choose = { "timeout"?: number | null; "real"?: boolean | null; "settle"?: boolean; "trigger": Target; "option": string }

export type Clear = { "kind"?: string | null }

export type ClearFakes = { "slot"?: string | null }

export type ClearLogs = { [key: string]: Json }

export type CloseDevMenu = { [key: string]: Json }

export type Count = { "target": Target; "timeout"?: number | null }

export type Deeplink = { "url": string; "timeout"?: number | null }

export type Delay = { "kind"?: "delay"; "by": number }

export type Dispatch = { "action": Json }

export type DoubleTap = { "timeout"?: number | null; "real"?: boolean | null; "settle"?: boolean; "target": Target }

export type Drag = { "timeout"?: number | null; "real"?: boolean | null; "settle"?: boolean; "source": Target; "to": Target }

export type Dump = { "kind": "dom" | "a11y" | "mhtml"; "name"?: string | null; "viewport"?: Viewport | null }

export type Eval = { "source": string; "arg"?: Json }

export type Exists = { "target": Target; "timeout"?: number | null }

export type ExpectTarget = { "target": Target; "assertion": IsVisible | IsHidden | IsEnabled | IsDisabled | IsChecked | HasText | HasValue | HasCount | HasAttr; "soft"?: boolean; "eventually"?: boolean; "timeout"?: number | null; "message"?: string | null }

export type Explore = { "budget": number; "steps"?: number | null; "depth"?: number; "seed"?: number | null; "allow"?: Array<Target>; "avoid"?: Array<Target>; "actions"?: Array<"tap" | "fill" | "scroll" | "swipe" | "back" | "key">; "fail_on"?: Array<"crash" | "page_error" | "console_error" | "failed_request" | "error_toast" | "layout_spill" | "horizontal_scroll" | "broken_image">; "checks"?: Array<CheckUse>; "label"?: string | null; "soft"?: boolean }

export type Fill = { "timeout"?: number | null; "real"?: boolean | null; "settle"?: boolean; "target": Target; "value": string; "submit"?: boolean }

export type Har = { "name"?: string | null; "whole"?: boolean }

export type HasAttr = { "equals"?: string | null; "contains"?: string | null; "matches"?: string | null; "kind"?: "attr"; "name": string }

export type HasCount = { "equals"?: number | null; "at_least"?: number | null; "at_most"?: number | null; "kind"?: "count" }

export type HasText = { "equals"?: string | null; "contains"?: string | null; "matches"?: string | null; "kind"?: "text" }

export type HasValue = { "equals"?: string | null; "contains"?: string | null; "matches"?: string | null; "kind"?: "value" }

export type Highlight = { "kind"?: "highlight"; "key"?: string | null; "window"?: Span | null; "hold"?: number | null; "keep"?: boolean; "placement"?: Placement | null; "style"?: { [key: string]: Json } | null; "emphasis"?: "none" | "freeze" | "slow"; "anchor": Target; "shape"?: "box" | "outline" | "rounded" }

export type Hold = { "duration": number; "reason"?: string | null }

export type Hover = { "timeout"?: number | null; "real"?: boolean | null; "settle"?: boolean; "target": Target }

export type Icon = { "kind"?: "icon"; "key"?: string | null; "window"?: Span | null; "hold"?: number | null; "keep"?: boolean; "placement"?: Placement | null; "style"?: { [key: string]: Json } | null; "emphasis"?: "none" | "freeze" | "slow"; "anchor": Target; "name": "check" | "cross" | "warning" | "info" | "tap" | "star" | "plus" | "minus" | "lock" | "bell" | "arrow-up" | "arrow-down" | "arrow-left" | "arrow-right" | "search" | "heart" }

export type Idle = { "timeout"?: number | null }

export type Image = { "kind"?: "image"; "key"?: string | null; "window"?: Span | null; "hold"?: number | null; "keep"?: boolean; "placement"?: Placement | null; "style"?: { [key: string]: Json } | null; "emphasis"?: "none" | "freeze" | "slow"; "anchor": Target; "image": { "path": string; "mime": string; "name"?: string | null }; "width"?: number | null }

export type Invoke = { "target": Target; "prop": string; "args"?: Array<Json> }

export type IsChecked = { "kind"?: "checked"; "on"?: boolean }

export type IsDisabled = { "kind"?: "disabled" }

export type IsEnabled = { "kind"?: "enabled" }

export type IsHidden = { "kind"?: "hidden" }

export type IsVisible = { "kind"?: "visible" }

export type Key = { "timeout"?: number | null; "real"?: boolean | null; "settle"?: boolean; "key": string; "target"?: Target | null }

export type Layout = { "target": Target; "timeout"?: number | null }

export type Logout = { [key: string]: Json }

export type Logs = { "kind"?: "console" | "errors" | "network" | "websocket"; "limit"?: number }

export type LongPress = { "timeout"?: number | null; "real"?: boolean | null; "settle"?: boolean; "target": Target; "hold"?: number | null }

export type Mount = { "component": string; "props"?: { [key: string]: Json } | null; "target"?: Target | null; "key"?: string | null }

export type Nav = { "route": string; "params"?: { [key: string]: Json } | null; "timeout"?: number | null }

export type NetProfile = { "latency": number; "down_kbps"?: number | null; "up_kbps"?: number | null; "loss"?: number; "offline"?: boolean }

export type OpenUrl = { "url": string; "timeout"?: number | null }

export type Pace = { "move": number; "dwell": number; "type_per_char": number; "hold": number; "screen_hold": number; "press": number }

export type Passthrough = { "kind"?: "passthrough" }

export type Patch = { "target": Target; "props"?: { [key: string]: Json } | null; "style"?: { [key: string]: Json } | null; "text"?: string | null; "key"?: string | null }

export type PatchJson = { "kind"?: "patch"; "path": string; "value": Json }

export type Placement = { "mode"?: "auto" | "force"; "side"?: "top" | "bottom" | "left" | "right" | null; "dx"?: number; "dy"?: number }

export type Pointer = { "target"?: Target | null; "duration"?: number | null }

export type ReadAttr = { "target": Target; "name": string; "timeout"?: number | null }

export type ReadText = { "target"?: Target | null; "timeout"?: number | null }

export type ReadValue = { "target": Target; "timeout"?: number | null }

export type Record = { "name": string; "pace"?: Pace | null; "viewport"?: Viewport | null; "pointer"?: boolean; "from_here"?: boolean; "focus"?: Target | null; "trim"?: boolean | null; "hide"?: Array<Target> }

export type Redact = { "kind"?: "redact"; "key"?: string | null; "window"?: Span | null; "hold"?: number | null; "keep"?: boolean; "placement"?: Placement | null; "style"?: { [key: string]: Json } | null; "emphasis"?: "none" | "freeze" | "slow"; "anchor": Target }

export type Reload = { "timeout"?: number | null }

export type Remove = { "key": string }

export type Respond = { "kind"?: "respond"; "status"?: number; "json"?: Json; "body"?: string | null; "headers"?: { [key: string]: string } | null }

export type Route = { "method"?: "*" | "GET" | "POST" | "PUT" | "PATCH" | "DELETE" | "HEAD" | "OPTIONS"; "pattern": string; "action": Delay | Respond | PatchJson | Abort | Passthrough; "times"?: number | null }

export type RouteNames = { [key: string]: Json }

export type Script = { "source": string; "when"?: "init" | "now" }

export type Scroll = { "timeout"?: number | null; "real"?: boolean | null; "settle"?: boolean; "target"?: Target | null; "to"?: "top" | "bottom" | "start" | "end" | Target | null; "by"?: [number, number] | null }

export type Select = { "timeout"?: number | null; "real"?: boolean | null; "settle"?: boolean; "target": Target; "value": string | Array<string> }

export type SessionSpec = { "key"?: string; "app": string; "platform": "web" | "ios" | "android"; "seed"?: string | null; "persona"?: string | null; "backend"?: string | null; "viewport"?: Viewport | null; "pace"?: string | null; "timeout"?: number | null; "noise"?: Array<string>; "root_seed"?: number | null; "now"?: string | null; "timezone"?: string | null; "locale"?: string | null; "permissions"?: "ask" | "grant" | "deny" | null; "network_ignore"?: Array<string>; "viewports"?: { [key: string]: Viewport }; "checks"?: Array<CheckUse>; "defaults"?: Array<CheckUse> }

export type SetViewport = { "name": string }

export type Settle = { "strict"?: boolean; "timeout"?: number | null }

export type Shape = { "profile": NetProfile }

export type Shot = { "name": string; "element"?: Target | null; "focus"?: Target | null; "full"?: boolean; "viewport"?: Viewport | null; "hide"?: Array<Target> }

export type Show = { "overlay": Arrow | Blur | Callout | Caption | Highlight | Icon | Image | Redact | Spotlight | Step | Title }

export type Span = { "start": number; "end": number }

export type Spotlight = { "kind"?: "spotlight"; "key"?: string | null; "window"?: Span | null; "hold"?: number | null; "keep"?: boolean; "placement"?: Placement | null; "style"?: { [key: string]: Json } | null; "emphasis"?: "none" | "freeze" | "slow"; "anchor": Target; "text"?: string | null; "dim"?: number | null }

export type State = { "path"?: string | null }

export type Step = { "kind"?: "step"; "key"?: string | null; "window"?: Span | null; "hold"?: number | null; "keep"?: boolean; "placement"?: Placement | null; "style"?: { [key: string]: Json } | null; "emphasis"?: "none" | "freeze" | "slow"; "anchor": Target; "index"?: number | null }

export type Style = { "css": string }

export type Submit = { "target": Target; "timeout"?: number | null }

export type Swipe = { "timeout"?: number | null; "real"?: boolean | null; "settle"?: boolean; "direction": "up" | "down" | "left" | "right"; "target"?: Target | null; "distance"?: number | null }

export type Take = { "session": SessionSpec; "span"?: Span | null; "lead_in"?: number; "warp"?: boolean; "size"?: [number, number] | null; "tolerance"?: number | null }

export type Tap = { "timeout"?: number | null; "real"?: boolean | null; "settle"?: boolean; "target": Target; "force"?: boolean }

export type Target = { "by": string; "args"?: { [key: string]: Json }; "within"?: Target | null; "near"?: Target | null; "nth"?: number | null; "surface"?: "app" | "native" | null }

export type Title = { "kind"?: "title"; "key"?: string | null; "window"?: Span | null; "hold"?: number | null; "keep"?: boolean; "placement"?: Placement | null; "style"?: { [key: string]: Json } | null; "emphasis"?: "none" | "freeze" | "slow"; "text": string; "subtitle"?: string | null; "kicker"?: string | null }

export type Toggle = { "target": Target; "value"?: boolean | null; "timeout"?: number | null }

export type Tree = { "target"?: Target | null; "max_lines"?: number | null; "timeout"?: number | null }

export type TypeText = { "timeout"?: number | null; "real"?: boolean | null; "settle"?: boolean; "target": Target; "value": string; "pace"?: string | null; "submit"?: boolean }

export type UiClear = { "timeout"?: number | null; "real"?: boolean | null; "settle"?: boolean; "target": Target }

export type UiRoute = { [key: string]: Json }

export type Unmount = { "key": string }

export type Unpatch = { "key": string }

export type Unroute = { "pattern"?: string | null }

export type Url = { [key: string]: Json }

export type Viewport = { "width": number; "height": number; "scale"?: number; "mobile"?: boolean; "touch"?: boolean; "name"?: string }

export type Visible = { "target": Target; "timeout"?: number | null }

export type WaitFor = { "target": Target; "state"?: "visible" | "attached" | "hidden" | "enabled"; "timeout"?: number | null }

export type WaitGone = { "target": Target; "timeout"?: number | null }

export type WaitUrl = { "pattern": string; "timeout"?: number | null }

export type Wheel = { "target"?: Target | null; "dx"?: number; "dy"?: number; "real"?: boolean | null }

export type CaptureDumpArgs = Dump

export type CaptureHarArgs = Har

export type CaptureHoldArgs = Hold

export type CaptureRecordArgs = Record

export type CaptureShotArgs = Shot

export type CheckCheckpointArgs = Checkpoint

export type CheckExploreArgs = Explore

export type ExpectTargetArgs = ExpectTarget

export type FakeArmArgs = Arm

export type FakeClearArgs = ClearFakes

export type InjectEvalArgs = Eval

export type InjectScriptArgs = Script

export type InjectStyleArgs = Style

export type MobileCloseDevMenuArgs = CloseDevMenu

export type MobileDispatchArgs = Dispatch

export type MobileIdleArgs = Idle

export type MobileInvokeArgs = Invoke

export type MobileLogoutArgs = Logout

export type MobileMountArgs = Mount

export type MobilePatchArgs = Patch

export type MobilePointerArgs = Pointer

export type MobileRouteNamesArgs = RouteNames

export type MobileSubmitArgs = Submit

export type MobileToggleArgs = Toggle

export type MobileUnmountArgs = Unmount

export type MobileUnpatchArgs = Unpatch

export type NetCassetteArgs = Cassette

export type NetRouteArgs = Route

export type NetShapeArgs = Shape

export type NetUnrouteArgs = Unroute

export type OverlayClearArgs = Clear

export type OverlayRemoveArgs = Remove

export type OverlayShowArgs = Show

export type ScoreTakeArgs = Take

export type SessionAsArgs = AsPersona

export type UiAttrArgs = ReadAttr

export type UiBackArgs = Back

export type UiCheckArgs = Check

export type UiChooseArgs = Choose

export type UiClearArgs = UiClear

export type UiCountArgs = Count

export type UiDeeplinkArgs = Deeplink

export type UiDoubleTapArgs = DoubleTap

export type UiDragArgs = Drag

export type UiExistsArgs = Exists

export type UiFillArgs = Fill

export type UiHoverArgs = Hover

export type UiKeyArgs = Key

export type UiLayoutArgs = Layout

export type UiLongPressArgs = LongPress

export type UiNavArgs = Nav

export type UiOpenUrlArgs = OpenUrl

export type UiReloadArgs = Reload

export type UiRouteArgs = UiRoute

export type UiScrollArgs = Scroll

export type UiSelectArgs = Select

export type UiSettleArgs = Settle

export type UiStateArgs = State

export type UiSwipeArgs = Swipe

export type UiTapArgs = Tap

export type UiTextArgs = ReadText

export type UiTreeArgs = Tree

export type UiTypeArgs = TypeText

export type UiUrlArgs = Url

export type UiValueArgs = ReadValue

export type UiVisibleArgs = Visible

export type UiWaitForArgs = WaitFor

export type UiWaitGoneArgs = WaitGone

export type UiWaitUrlArgs = WaitUrl

export type WebClearLogsArgs = ClearLogs

export type WebLogsArgs = Logs

export type WebViewportArgs = SetViewport

export type WebWheelArgs = Wheel

export interface OpArgs {
  "capture.dump": CaptureDumpArgs
  "capture.har": CaptureHarArgs
  "capture.hold": CaptureHoldArgs
  "capture.record": CaptureRecordArgs
  "capture.shot": CaptureShotArgs
  "check.checkpoint": CheckCheckpointArgs
  "check.explore": CheckExploreArgs
  "expect.target": ExpectTargetArgs
  "fake.arm": FakeArmArgs
  "fake.clear": FakeClearArgs
  "inject.eval": InjectEvalArgs
  "inject.script": InjectScriptArgs
  "inject.style": InjectStyleArgs
  "mobile.close_dev_menu": MobileCloseDevMenuArgs
  "mobile.dispatch": MobileDispatchArgs
  "mobile.idle": MobileIdleArgs
  "mobile.invoke": MobileInvokeArgs
  "mobile.logout": MobileLogoutArgs
  "mobile.mount": MobileMountArgs
  "mobile.patch": MobilePatchArgs
  "mobile.pointer": MobilePointerArgs
  "mobile.route_names": MobileRouteNamesArgs
  "mobile.submit": MobileSubmitArgs
  "mobile.toggle": MobileToggleArgs
  "mobile.unmount": MobileUnmountArgs
  "mobile.unpatch": MobileUnpatchArgs
  "net.cassette": NetCassetteArgs
  "net.route": NetRouteArgs
  "net.shape": NetShapeArgs
  "net.unroute": NetUnrouteArgs
  "overlay.clear": OverlayClearArgs
  "overlay.remove": OverlayRemoveArgs
  "overlay.show": OverlayShowArgs
  "score.take": ScoreTakeArgs
  "session.as": SessionAsArgs
  "ui.attr": UiAttrArgs
  "ui.back": UiBackArgs
  "ui.check": UiCheckArgs
  "ui.choose": UiChooseArgs
  "ui.clear": UiClearArgs
  "ui.count": UiCountArgs
  "ui.deeplink": UiDeeplinkArgs
  "ui.double_tap": UiDoubleTapArgs
  "ui.drag": UiDragArgs
  "ui.exists": UiExistsArgs
  "ui.fill": UiFillArgs
  "ui.hover": UiHoverArgs
  "ui.key": UiKeyArgs
  "ui.layout": UiLayoutArgs
  "ui.long_press": UiLongPressArgs
  "ui.nav": UiNavArgs
  "ui.open_url": UiOpenUrlArgs
  "ui.reload": UiReloadArgs
  "ui.route": UiRouteArgs
  "ui.scroll": UiScrollArgs
  "ui.select": UiSelectArgs
  "ui.settle": UiSettleArgs
  "ui.state": UiStateArgs
  "ui.swipe": UiSwipeArgs
  "ui.tap": UiTapArgs
  "ui.text": UiTextArgs
  "ui.tree": UiTreeArgs
  "ui.type": UiTypeArgs
  "ui.url": UiUrlArgs
  "ui.value": UiValueArgs
  "ui.visible": UiVisibleArgs
  "ui.wait_for": UiWaitForArgs
  "ui.wait_gone": UiWaitGoneArgs
  "ui.wait_url": UiWaitUrlArgs
  "web.clear_logs": WebClearLogsArgs
  "web.logs": WebLogsArgs
  "web.viewport": WebViewportArgs
  "web.wheel": WebWheelArgs
}
