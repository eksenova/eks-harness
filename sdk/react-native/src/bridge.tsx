import { NavigationRouteContext } from "@react-navigation/native";
import React from "react";
import {
  Alert,
  Dimensions,
  InteractionManager,
  LogBox,
  NativeModules,
  PixelRatio,
  Platform,
  StyleSheet,
  TextInput,
  View,
} from "react-native";
import { configure, harnessOptions, navigationAdapter, themeOf, type HarnessOptions } from "./config";
import { registeredFakes } from "./fakes";
import { Box, markerFrame, relativeTo, revealOffset } from "./geometry";
import { HarnessOverlay, overlay } from "./overlay";
import { harnessEnabled, harnessReport, harnessUrl } from "./runtime";

export const REPLY_BINDING = "__ehxReply";
export const BRIDGE_GLOBAL = "__ehxBridge";

type Fiber = {
  tag: number;
  type: any;
  elementType: any;
  key: string | null;
  memoizedProps: any;
  stateNode: any;
  child: Fiber | null;
  sibling: Fiber | null;
  return: Fiber | null;
};

type NodeKind = "screen" | "button" | "input" | "switch" | "text" | "view";

type UiNode = {
  kind: NodeKind;
  fiber: Fiber;
  handler?: Fiber;
  id?: string;
  label?: string;
  texts: string[];
  value?: string;
  placeholder?: string;
  secure?: boolean;
  disabled?: boolean;
  checked?: boolean;
  depth: number;
  children: UiNode[];
};

type Target =
  | string
  | {
      testID?: string;
      id?: string;
      text?: string;
      label?: string;
      placeholder?: string;
      kind?: NodeKind;
      exact?: boolean;
      nth?: number;
      near?: string;
      focused?: boolean;
    };

const HOST_COMPONENT = 5;
const HOST_TEXT = 6;
const CONTEXT_PROVIDER = 10;
const CLASS_COMPONENT = 1;

const TEXT_HOSTS = new Set(["RCTText", "RCTVirtualText"]);
const INPUT_HOSTS = new Set([
  "RCTSinglelineTextInputView",
  "RCTMultilineTextInputView",
  "AndroidTextInput",
]);
const SWITCH_HOSTS = new Set(["RCTSwitch", "AndroidSwitch"]);

let rootFiberHolder: Fiber | null = null;

const navigationRef = {
  isReady: () => navigationAdapter().active() !== null,
  getRootState: () => navigationAdapter().active()?.getRootState(),
  getCurrentRoute: () => navigationAdapter().active()?.getCurrentRoute(),
  navigate: (...args: unknown[]) => (navigationAdapter().active() as any)?.navigate(...args),
  canGoBack: () => Boolean(navigationAdapter().active()?.canGoBack()),
  goBack: () => navigationAdapter().active()?.goBack(),
  container: () => navigationAdapter().container(),
};
let inflight = 0;
let lastNetwork = Date.now();
let lastCommit = Date.now();
let started = false;

type Marker = Box & { key: number };

type MarkerHost = {
  show: (marker: Marker) => Promise<void>;
  clear: () => void;
  origin: () => any;
};

let markerHost: MarkerHost | null = null;

export class HarnessRoot extends React.Component<
  { children: React.ReactNode },
  { marker: Marker | null }
> {
  state = { marker: null as Marker | null };

  private host = React.createRef<View>();

  private laidOut: (() => void) | null = null;

  componentDidMount() {
    rootFiberHolder = (this as any)._reactInternals ?? null;
    markerHost = {
      show: (marker) =>
        new Promise<void>((resolve) => {
          this.settleLayout();
          this.laidOut = resolve;
          this.setState({ marker });
        }),
      clear: () => {
        this.settleLayout();
        this.setState({ marker: null });
      },
      origin: () => this.host.current,
    };
  }

  componentWillUnmount() {
    this.settleLayout();
    markerHost = null;
  }

  private settleLayout = () => {
    const resolve = this.laidOut;
    this.laidOut = null;
    resolve?.();
  };

  render() {
    const { marker } = this.state;
    return (
      <View ref={this.host} collapsable={false} style={styles.fill}>
        {this.props.children}
        {marker ? (
          <View
            key={marker.key}
            pointerEvents="none"
            onLayout={this.settleLayout}
            style={[markerStyle() as any, markerFrame(marker)]}
          />
        ) : null}
        <HarnessOverlay />
      </View>
    );
  }
}

const styles = { fill: { flex: 1 } } as const;

function markerStyle() {
  const theme = themeOf();
  return { position: "absolute", borderWidth: 3, borderRadius: 10, borderColor: theme.marker, backgroundColor: theme.markerFill };
}

const MARKER_MOUNT_TIMEOUT_MS = 500;

async function markTouch(fiber: Fiber, holdMs = 0) {
  if (!markerHost) return;
  const box = await layoutOf(fiber);
  if (!box || !markerHost) return;
  await Promise.race([markerHost.show({ ...box, key: Date.now() }), sleep(MARKER_MOUNT_TIMEOUT_MS)]);
  await nextTwoFrames();
  if (holdMs > 0) await sleep(holdMs);
}

function clearMarker() {
  markerHost?.clear();
}

async function glideTo(fiber: Fiber, glideMs: unknown) {
  const host = overlay();
  if (!host || typeof glideMs !== "number" || glideMs < 0) return null;
  const box = await layoutOf(fiber);
  if (!box) return null;
  const x = box.x + box.width / 2;
  const y = box.y + box.height / 2;
  await host.glide(x, y, glideMs);
  return { x, y };
}

const anchors = new Map<string, Target>();
let anchorTimer: ReturnType<typeof setInterval> | null = null;

async function refreshAnchors() {
  const host = overlay();
  if (!host) return;
  for (const [key, target] of anchors) {
    const node = findAll(target)[0];
    host.move(key, node ? await layoutOf(node.fiber) : null);
  }
}

function trackAnchors() {
  if (anchorTimer || anchors.size === 0) return;
  anchorTimer = setInterval(() => {
    if (anchors.size === 0) {
      if (anchorTimer) clearInterval(anchorTimer);
      anchorTimer = null;
      return;
    }
    refreshAnchors();
  }, 150);
}

const HARNESS_PRESS_MS = 250;

function pressHold(pressMs: unknown) {
  return typeof pressMs === "number" && Number.isFinite(pressMs) && pressMs >= 0 ? pressMs : HARNESS_PRESS_MS;
}

function nextTwoFrames(): Promise<void> {
  return new Promise((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
  });
}

function captureClean(): { marker: string; logBox: string; shakeMenu: string } {
  clearMarker();
  LogBox.ignoreAllLogs(true);
  let shakeMenu = "n/a";
  if (Platform.OS === "ios") {
    try {
      NativeModules.DevSettings?.setIsShakeToShowDevMenuEnabled?.(false);
      shakeMenu = "disabled";
    } catch {
      shakeMenu = "unavailable";
    }
  }
  return { marker: "cleared", logBox: "ignored", shakeMenu };
}

function committedRoot(): Fiber | null {
  let fiber = rootFiberHolder;
  if (!fiber) return null;
  while (fiber.return) fiber = fiber.return;
  return fiber.stateNode?.current ?? fiber;
}

const ICON_GLYPHS = /[\uE000-\uF8FF]|[\uDB80-\uDBFF][\uDC00-\uDFFF]/g;

function clean(text: string): string {
  return text.replace(ICON_GLYPHS, "").replace(/\s+/g, " ").trim();
}

function fold(text: string): string {
  const locale = harnessOptions().locale;
  return text
    .toLocaleLowerCase(locale)
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/\u0131/g, "i")
    .replace(/\s+/g, " ")
    .trim();
}

function routeFocus(): Map<string, boolean> {
  const focus = new Map<string, boolean>();
  const visit = (state: any) => {
    if (!state?.routes) return;
    const focusedKey = state.routes[state.index ?? 0]?.key;
    for (const route of state.routes) {
      focus.set(route.key, route.key === focusedKey);
      visit(route.state);
    }
  };
  visit(navigationRef.isReady() ? navigationRef.getRootState() : null);
  return focus;
}

function routeOfProvider(fiber: Fiber): { key: string; name: string } | null {
  if (fiber.tag !== CONTEXT_PROVIDER) return null;
  const context = fiber.type?._context ?? fiber.type;
  if (context !== NavigationRouteContext) return null;
  const route = fiber.memoizedProps?.value;
  return route?.key ? { key: route.key, name: route.name } : null;
}

function hostTextOf(fiber: Fiber): string {
  const parts: string[] = [];
  const visit = (node: Fiber | null) => {
    while (node) {
      if (node.tag === HOST_TEXT && typeof node.memoizedProps === "string") {
        parts.push(node.memoizedProps);
      } else {
        visit(node.child);
      }
      node = node.sibling;
    }
  };
  if (fiber.tag === HOST_TEXT) return String(fiber.memoizedProps ?? "");
  visit(fiber.child);
  return clean(parts.join(""));
}

function nearestHandler(fiber: Fiber, prop: string): Fiber | null {
  let node: Fiber | null = fiber;
  let hops = 0;
  while (node && hops < 12) {
    if (typeof node.memoizedProps?.[prop] === "function") return node;
    node = node.return;
    hops++;
  }
  return null;
}

function buildTree(options: { all?: boolean } = {}): UiNode[] {
  const root = committedRoot();
  if (!root) return [];
  const focus = routeFocus();
  const seenRoutes = new Set<string>();
  const top: UiNode[] = [];

  const add = (list: UiNode[], node: UiNode) => {
    list.push(node);
    return node;
  };

  const walk = (
    fiber: Fiber | null,
    out: UiNode[],
    depth: number,
    button: UiNode | null,
    press: unknown
  ) => {
    while (fiber) {
      visit(fiber, out, depth, button, press);
      fiber = fiber.sibling;
    }
  };

  const visit = (
    fiber: Fiber,
    out: UiNode[],
    depth: number,
    button: UiNode | null,
    press: unknown
  ) => {
    const props = fiber.memoizedProps ?? {};
    const route = routeOfProvider(fiber);
    if (route) {
      if (!options.all && focus.get(route.key) === false) return;
      if (seenRoutes.has(route.key)) {
        walk(fiber.child, out, depth, button, press);
        return;
      }
      seenRoutes.add(route.key);
      const screen = add(out, {
        kind: "screen",
        fiber,
        label: route.name,
        texts: [],
        depth,
        children: [],
      });
      walk(fiber.child, screen.children, depth + 1, button, press);
      return;
    }

    if (fiber.tag === HOST_COMPONENT) {
      const hostType = String(fiber.type);
      if (INPUT_HOSTS.has(hostType)) {
        const handler =
          nearestHandler(fiber, "onChangeText") ?? nearestHandler(fiber, "onChange");
        const owner = handler?.memoizedProps ?? {};
        add(button ? button.children : out, {
          kind: "input",
          fiber,
          handler: handler ?? undefined,
          id: props.testID ?? owner.testID,
          label: props.accessibilityLabel ?? owner.accessibilityLabel,
          placeholder: props.placeholder ?? owner.placeholder,
          value: String(owner.value ?? props.text ?? props.value ?? owner.defaultValue ?? ""),
          secure: Boolean(props.secureTextEntry ?? owner.secureTextEntry),
          disabled: owner.editable === false || props.editable === false,
          texts: [],
          depth,
          children: [],
        });
        return;
      }
      if (SWITCH_HOSTS.has(hostType)) {
        const handler = nearestHandler(fiber, "onValueChange");
        add(button ? button.children : out, {
          kind: "switch",
          fiber,
          handler: handler ?? undefined,
          id: props.testID ?? handler?.memoizedProps?.testID,
          label: props.accessibilityLabel,
          checked: Boolean(props.value ?? handler?.memoizedProps?.value),
          disabled: Boolean(props.disabled ?? handler?.memoizedProps?.disabled),
          texts: [],
          depth,
          children: [],
        });
        return;
      }
      if (TEXT_HOSTS.has(hostType)) {
        const text = hostTextOf(fiber);
        if (text) {
          if (button) button.texts.push(text);
          else add(out, { kind: "text", fiber, texts: [text], depth, children: [] });
        }
        return;
      }
      if (button && !button.id && props.testID) button.id = props.testID;
      if (!button && props.testID) {
        const view = add(out, {
          kind: "view",
          fiber,
          id: props.testID,
          label: props.accessibilityLabel,
          texts: [],
          depth,
          children: [],
        });
        walk(fiber.child, view.children, depth + 1, button, press);
        return;
      }
    } else if (fiber.tag === HOST_TEXT) {
      const text = clean(String(fiber.memoizedProps ?? ""));
      if (text) {
        if (button) button.texts.push(text);
        else add(out, { kind: "text", fiber, texts: [text], depth, children: [] });
      }
      return;
    } else if (typeof props.onPress === "function" && props.onPress !== press) {
      const node = add(button ? button.children : out, {
        kind: "button",
        fiber,
        handler: fiber,
        id: props.testID ?? props.nativeID,
        label: clean(props.accessibilityLabel ?? (typeof props.title === "string" ? props.title : "")) || undefined,
        disabled: Boolean(props.disabled || props.accessibilityState?.disabled),
        texts: [],
        depth,
        children: [],
      });
      walk(fiber.child, node.children, depth + 1, node, props.onPress);
      return;
    }

    walk(fiber.child, out, depth, button, press);
  };

  walk(root.child, top, 0, null, undefined);
  return top;
}

function flatten(nodes: UiNode[], into: UiNode[] = []): UiNode[] {
  for (const node of nodes) {
    into.push(node);
    flatten(node.children, into);
  }
  return into;
}

function labelOf(node: UiNode): string {
  if (node.kind === "text") return clean(node.texts.join(" "));
  return clean(node.label ?? node.texts.join(" "));
}

function describe(node: UiNode): string {
  const id = node.id ? `#${node.id}` : "";
  const label = labelOf(node);
  const quoted = label ? ` "${label.length > 90 ? `${label.slice(0, 87)}...` : label}"` : "";
  const flags = node.disabled ? " (disabled)" : "";
  switch (node.kind) {
    case "screen":
      return `[screen ${node.label}]`;
    case "input": {
      const shown = node.secure ? (node.value ? "********" : "") : node.value ?? "";
      const hint = node.placeholder ? ` placeholder="${node.placeholder}"` : "";
      return `input${id}${quoted}${hint} = "${shown}"${flags}`;
    }
    case "switch":
      return `switch${id}${quoted} ${node.checked ? "on" : "off"}${flags}`;
    case "button":
      return `button${id}${quoted}${flags}`;
    case "view":
      return `view${id}${quoted}`;
    default:
      return `text${quoted}`;
  }
}

function renderTree(nodes: UiNode[], maxLines: number): string {
  const lines: string[] = [];
  const emit = (list: UiNode[], indent: number) => {
    for (const node of list) {
      if (node.kind === "view" && node.children.length === 0 && !node.label) {
        lines.push(`${"  ".repeat(indent)}${describe(node)}`);
        continue;
      }
      lines.push(`${"  ".repeat(indent)}${describe(node)}`);
      emit(node.children, indent + 1);
    }
  };
  emit(nodes, 0);
  if (lines.length > maxLines) {
    return `${lines.slice(0, maxLines).join("\n")}\n... ${lines.length - maxLines} more lines (tree({ all: true }) or a narrower screen)`;
  }
  return lines.join("\n");
}

function parseTarget(target: Target) {
  if (typeof target !== "string") return target;
  if (target === ":focused") return { focused: true };
  const match = target.match(/^(text|label|placeholder|testid|id)=(.*)$/s);
  if (match) {
    const [, kind, rest] = match;
    if (kind === "testid" || kind === "id") return { testID: rest };
    return { [kind]: rest } as Record<string, string>;
  }
  if (target.startsWith("#")) return { testID: target.slice(1) };
  return { text: target };
}

function headingBefore(nodes: UiNode[], node: UiNode): string {
  const index = nodes.indexOf(node);
  for (let i = index - 1; i >= 0; i--) {
    if (nodes[i].kind === "text" && labelOf(nodes[i])) return labelOf(nodes[i]);
  }
  return "";
}

function findAll(target: Target, preferred?: NodeKind[]): UiNode[] {
  const spec: any = parseTarget(target);
  const nodes = flatten(buildTree()).filter((node) => node.kind !== "screen");
  const ranked = rankMatches(nodes, spec, target, preferred);
  if (!spec.near) return ranked;
  const needle = fold(spec.near);
  const near = ranked.filter((node) => fold(headingBefore(nodes, node)).includes(needle));
  return near.length ? near : ranked;
}

function ambiguity(target: Target, preferred?: NodeKind[]): string | null {
  const spec: any = parseTarget(target);
  if (spec.nth !== undefined || spec.near) return null;
  const nodes = flatten(buildTree()).filter((node) => node.kind !== "screen");
  const ranked = rankMatches(nodes, spec, target, preferred);
  if (ranked.length < 2) return null;
  const best = ranked[0];
  const sameTier = ranked.filter(
    (node) =>
      node.kind === best.kind &&
      (node.id ?? "") === (best.id ?? "") &&
      fold(labelOf(node)) === fold(labelOf(best))
  );
  if (sameTier.length < 2) return null;
  const list = sameTier
    .slice(0, 8)
    .map((node, index) => `  [${index}] ${describe(node)} (after "${headingBefore(nodes, node).slice(0, 60)}")`)
    .join("\n");
  return `${sameTier.length} elements match ${JSON.stringify(target)} equally well on ${currentRouteName()}:\n${list}\nPick one with { text, near: '<heading above it>' } or { text, nth }, or give it a testID.`;
}

function rankMatches(nodes: UiNode[], spec: any, target: Target, preferred?: NodeKind[]): UiNode[] {
  const wanted = spec.testID ?? spec.id;
  let matches: UiNode[];
  if (spec.focused) {
    const focused = TextInput.State?.currentlyFocusedInput?.();
    matches = focused ? nodes.filter((node) => node.kind === "input" && publicInstance(node.fiber) === focused) : [];
  } else if (wanted) {
    matches = nodes.filter((node) => node.id === wanted);
  } else if (spec.placeholder) {
    const needle = fold(spec.placeholder);
    matches = nodes.filter((node) => node.placeholder && fold(node.placeholder).includes(needle));
  } else {
    const needle = fold(String(spec.text ?? spec.label ?? ""));
    if (!needle && spec.kind) return nodes.filter((node) => node.kind === spec.kind);
    if (!needle) throw new Error(`empty target ${JSON.stringify(target)}`);
    const candidates = nodes.filter((node) => {
      const haystack = fold(
        [labelOf(node), node.placeholder ?? "", spec.label ? "" : node.texts.join(" ")].join(" ")
      );
      return spec.exact ? fold(labelOf(node)) === needle : haystack.includes(needle);
    });
    const exact = candidates.filter((node) => fold(labelOf(node)) === needle);
    matches = exact.length ? [...exact, ...candidates.filter((node) => !exact.includes(node))] : candidates;
  }
  if (spec.kind) matches = matches.filter((node) => node.kind === spec.kind);
  if (preferred?.length) {
    const first = matches.filter((node) => preferred.includes(node.kind));
    matches = [...first, ...matches.filter((node) => !preferred.includes(node.kind))];
  }
  return matches;
}

function findOne(target: Target, preferred?: NodeKind[]): UiNode {
  const ambiguous = ambiguity(target, preferred);
  if (ambiguous) throw new Error(ambiguous);
  const matches = findAll(target, preferred);
  const spec: any = parseTarget(target);
  const node = matches[spec.nth ?? 0];
  if (!node) {
    throw new Error(`no element matches ${JSON.stringify(target)} on ${currentRouteName()}`);
  }
  return node;
}

function enclosingButton(node: UiNode): UiNode | null {
  let fiber: Fiber | null = node.fiber;
  let hops = 0;
  while (fiber && hops < 25) {
    if (typeof fiber.memoizedProps?.onPress === "function") {
      return { ...node, kind: "button", handler: fiber, disabled: Boolean(fiber.memoizedProps.disabled) };
    }
    fiber = fiber.return;
    hops++;
  }
  return null;
}

const syntheticEvent = (extra: Record<string, unknown> = {}) => ({
  nativeEvent: { timestamp: Date.now(), ...extra },
  persist() {},
  preventDefault() {},
  stopPropagation() {},
  isDefaultPrevented: () => false,
  isPropagationStopped: () => false,
  currentTarget: null,
  target: null,
});

function pathTo(state: any, name: string): string[] | null {
  if (!state) return null;
  if (state.routeNames?.includes(name)) return [];
  const focused = state.routes?.[state.index ?? 0];
  const ordered = focused ? [focused, ...state.routes.filter((route: any) => route !== focused)] : state.routes ?? [];
  for (const route of ordered) {
    const inner = pathTo(route.state, name);
    if (inner) return [route.name, ...inner];
  }
  return null;
}

function reachableRouteNames(state: any, into: Set<string> = new Set()): string[] {
  if (!state) return [...into];
  for (const routeName of state.routeNames ?? []) into.add(routeName);
  for (const route of state.routes ?? []) reachableRouteNames(route.state, into);
  return [...into];
}

function focusedNames(): string[] {
  const names: string[] = [];
  let state: any = navigationRef.isReady() ? navigationRef.getRootState() : null;
  while (state?.routes) {
    const route = state.routes[state.index ?? 0];
    if (!route) break;
    names.push(route.name);
    state = route.state;
  }
  return names;
}

function currentRouteName(): string {
  return navigationRef.isReady() ? navigationRef.getCurrentRoute()?.name ?? "?" : "(navigation not ready)";
}

function publicInstance(fiber: Fiber): any {
  let node: Fiber | null = fiber;
  while (node && node.tag !== HOST_COMPONENT) node = node.child;
  const stateNode = node?.stateNode;
  return stateNode?.canonical?.publicInstance ?? stateNode?.canonical ?? stateNode ?? null;
}

function measureInWindow(instance: any): Promise<Box | null> {
  if (!instance?.measureInWindow) return Promise.resolve(null);
  return new Promise((resolve) => {
    const timer = setTimeout(() => resolve(null), 1000);
    instance.measureInWindow((x: number, y: number, width: number, height: number) => {
      clearTimeout(timer);
      resolve({ x, y, width, height });
    });
  });
}

async function layoutOf(fiber: Fiber): Promise<Box | null> {
  const box = await measureInWindow(publicInstance(fiber));
  if (!box) return null;
  const origin = markerHost ? await measureInWindow(markerHost.origin()) : null;
  return relativeTo(box, origin);
}

function measureWithin(instance: any, ancestor: any): Promise<Box | null> {
  if (!instance?.measureLayout || !ancestor) return Promise.resolve(null);
  return new Promise((resolve) => {
    const timer = setTimeout(() => resolve(null), 1000);
    instance.measureLayout(
      ancestor,
      (x: number, y: number, width: number, height: number) => {
        clearTimeout(timer);
        resolve({ x, y, width, height });
      },
      () => {
        clearTimeout(timer);
        resolve(null);
      }
    );
  });
}

function scrollViewsAround(fiber: Fiber): any[] {
  const out: any[] = [];
  for (let node = fiber.return; node; node = node.return) {
    const instance = node.stateNode;
    if (
      node.tag === CLASS_COMPONENT &&
      typeof instance?.getInnerViewRef === "function" &&
      typeof instance?.getNativeScrollRef === "function" &&
      typeof instance?.scrollTo === "function"
    ) {
      out.push(instance);
    }
  }
  return out;
}

const REVEAL_SETTLE_MS = 900;

async function settledBox(instance: any): Promise<Box | null> {
  const start = Date.now();
  let last = await measureInWindow(instance);
  while (Date.now() - start < REVEAL_SETTLE_MS) {
    await nextTwoFrames();
    const box = await measureInWindow(instance);
    if (box && last && Math.abs(box.x - last.x) < 0.5 && Math.abs(box.y - last.y) < 0.5) return box;
    last = box;
  }
  return last;
}

async function reveal(fiber: Fiber): Promise<boolean> {
  const target = publicInstance(fiber);
  if (!target) return false;
  const window = Dimensions.get("window");
  let moved = false;
  for (const scroll of scrollViewsAround(fiber)) {
    const content = scroll.getInnerViewRef();
    const [box, frame, inner, within] = await Promise.all([
      measureInWindow(target),
      measureInWindow(scroll.getNativeScrollRef()),
      measureInWindow(content),
      measureWithin(target, content),
    ]);
    if (!box || !frame || !inner || !within) continue;
    const horizontal = Boolean(scroll.props?.horizontal);
    const offset = horizontal
      ? revealOffset(
          { start: box.x, size: box.width },
          { start: frame.x, size: frame.width },
          { start: 0, size: window.width },
          within.x,
          inner.width
        )
      : revealOffset(
          { start: box.y, size: box.height },
          { start: frame.y, size: frame.height },
          { start: 0, size: window.height },
          within.y,
          inner.height
        );
    if (offset === null) continue;
    scroll.scrollTo(horizontal ? { x: offset, animated: true } : { y: offset, animated: true });
    await settledBox(target);
    moved = true;
  }
  return moved;
}

function scrollableFrom(fiber: Fiber | null, downward: boolean): any {
  const isScrollable = (node: Fiber) =>
    node.tag === CLASS_COMPONENT &&
    (typeof node.stateNode?.scrollToEnd === "function" || typeof node.stateNode?.scrollToOffset === "function");
  if (!downward) {
    let node = fiber;
    while (node) {
      if (isScrollable(node)) return node.stateNode;
      node = node.return;
    }
    return null;
  }
  const queue: Fiber[] = fiber ? [fiber] : [];
  while (queue.length) {
    const node = queue.shift()!;
    if (isScrollable(node)) return node.stateNode;
    let child = node.child;
    while (child) {
      queue.push(child);
      child = child.sibling;
    }
  }
  return null;
}

function focusedScreenFiber(): Fiber | null {
  const screens = flatten(buildTree()).filter((node) => node.kind === "screen");
  return screens.length ? screens[screens.length - 1].fiber : committedRoot();
}

function readPath(value: any, path?: string) {
  if (!path) return value;
  return path.split(".").reduce((acc, key) => (acc == null ? acc : acc[key]), value);
}

function jsonSafe(value: any, depth = 0): any {
  if (value === undefined || value === null) return value ?? null;
  if (typeof value === "function") return `[function ${value.name || "anonymous"}]`;
  if (typeof value !== "object") return value;
  if (depth > 6) return "[...]";
  if (Array.isArray(value)) return value.slice(0, 200).map((item) => jsonSafe(item, depth + 1));
  const out: Record<string, unknown> = {};
  for (const [key, inner] of Object.entries(value).slice(0, 200)) out[key] = jsonSafe(inner, depth + 1);
  return out;
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

const RENDER_LOOP_AFTER_MS = 600;
const RENDER_LOOP_STEADY_MS = 300;

async function waitIdle({ timeout = 8000, quiet = 250, renderQuiet = 150 } = {}) {
  const start = Date.now();
  await new Promise<void>((resolve) => {
    const handle = InteractionManager.runAfterInteractions(() => resolve());
    setTimeout(() => {
      handle.cancel?.();
      resolve();
    }, Math.min(timeout, 2000));
  });
  let steady: { at: number; tree: string } | null = null;
  while (Date.now() - start < timeout) {
    const now = Date.now();
    const networkQuiet = inflight <= 0 && now - lastNetwork >= quiet;
    if (networkQuiet && now - lastCommit >= renderQuiet) {
      return { ms: Date.now() - start, timedOut: false };
    }
    if (networkQuiet && now - start >= RENDER_LOOP_AFTER_MS) {
      const tree = renderTree(buildTree(), 400);
      if (!steady || steady.tree !== tree) {
        steady = { at: now, tree };
      } else if (now - steady.at >= RENDER_LOOP_STEADY_MS) {
        return { ms: Date.now() - start, timedOut: false, renderLoop: true };
      }
    }
    await sleep(40);
  }
  const end = Date.now();
  return { ms: end - start, timedOut: true, inflight, sinceNetworkMs: end - lastNetwork, sinceCommitMs: end - lastCommit };
}

async function waitFor(target: Target, { timeout = 10000, gone = false } = {}) {
  const start = Date.now();
  let lastError = "";
  while (Date.now() - start < timeout) {
    try {
      const found = findAll(target).length > 0;
      if (found !== gone) return { ms: Date.now() - start };
    } catch (error: any) {
      lastError = error?.message ?? String(error);
    }
    await sleep(50);
  }
  throw new Error(
    `timed out after ${timeout}ms waiting for ${JSON.stringify(target)} to ${gone ? "disappear" : "appear"} on ${currentRouteName()}${lastError ? ` (${lastError})` : ""}`
  );
}

function snapshotInfo(maxLines = 160) {
  return {
    route: `${currentRouteName()} (${navigationRef.container()} navigator)`,
    params: navigationRef.isReady() ? jsonSafe(navigationRef.getCurrentRoute()?.params ?? null) : null,
    tree: renderTree(buildTree(), maxLines),
  };
}

const commands: Record<string, (args: any) => Promise<unknown> | unknown> = {
  ping: () => ({ platform: Platform.OS, route: currentRouteName(), navigator: navigationRef.container() }),
  boot: () => captureClean(),
  tree: ({ all, maxLines } = {}) => ({
    route: currentRouteName(),
    tree: renderTree(buildTree({ all }), maxLines ?? 250),
  }),
  snapshot: ({ maxLines } = {}) => snapshotInfo(maxLines),
  find: ({ target }) =>
    findAll(target).slice(0, 20).map((node) => ({ line: describe(node), kind: node.kind, id: node.id })),
  exists: ({ target }) => findAll(target).length > 0,
  text: ({ target }) => {
    if (!target) return flatten(buildTree()).map(labelOf).filter(Boolean).join("\n");
    const node = findOne(target);
    return [labelOf(node), ...flatten(node.children).map(labelOf)].filter(Boolean).join("\n");
  },
  press: async ({ target, long, pressMs, glideMs, mark }) => {
    let node = findOne(target, ["button", "switch"]);
    if (node.kind === "switch") return commands.toggle({ target, pressMs });
    if (node.kind !== "button") {
      const wrapper = enclosingButton(node);
      if (!wrapper) throw new Error(`${describe(node)} is not pressable`);
      node = wrapper;
    }
    if (node.disabled) throw new Error(`${describe(node)} is disabled`);
    const props = node.handler!.memoizedProps;
    const fn = long ? props.onLongPress : props.onPress;
    if (typeof fn !== "function") throw new Error(`${describe(node)} has no ${long ? "onLongPress" : "onPress"}`);
    await reveal(node.fiber);
    const touch = await glideTo(node.fiber, glideMs);
    if (touch) {
      overlay()?.tap(touch.x, touch.y);
      await sleep(pressHold(pressMs));
    } else if (mark !== false) {
      await markTouch(node.fiber, pressHold(pressMs));
      clearMarker();
    }
    props.onPressIn?.(syntheticEvent());
    await fn(syntheticEvent());
    props.onPressOut?.(syntheticEvent());
    return describe(node);
  },
  fill: async ({ target, text, submit, blur, glideMs, mark }) => {
    const node = findOne(target, ["input"]);
    if (node.kind !== "input") throw new Error(`${describe(node)} is not a text input`);
    if (node.disabled) throw new Error(`${describe(node)} is not editable`);
    await reveal(node.fiber);
    const touch = await glideTo(node.fiber, glideMs);
    if (touch) overlay()?.tap(touch.x, touch.y);
    else if (mark !== false) await markTouch(node.fiber);
    const owner = node.handler?.memoizedProps ?? {};
    const value = String(text ?? "");
    owner.onFocus?.(syntheticEvent());
    if (typeof owner.onChangeText === "function") owner.onChangeText(value);
    else if (typeof owner.onChange === "function") owner.onChange(syntheticEvent({ text: value }));
    await sleep(32);
    const fresh = findAll(target, ["input"])[0] ?? node;
    const controlled = fresh.handler?.memoizedProps && "value" in fresh.handler.memoizedProps;
    if (!controlled) publicInstance(fresh.fiber)?.setNativeProps?.({ text: value });
    await nextTwoFrames();
    clearMarker();
    if (submit) owner.onSubmitEditing?.(syntheticEvent({ text: value }));
    if (blur || submit) {
      owner.onEndEditing?.(syntheticEvent({ text: value }));
      owner.onBlur?.(syntheticEvent());
    }
    return `filled ${describe({ ...node, value })}`;
  },
  submit: ({ target }) => {
    const node = findOne(target, ["input"]);
    const owner = nearestHandler(node.fiber, "onSubmitEditing")?.memoizedProps;
    if (!owner) throw new Error(`${describe(node)} has no onSubmitEditing`);
    owner.onSubmitEditing(syntheticEvent({ text: node.value }));
    return describe(node);
  },
  toggle: async ({ target, value, pressMs }) => {
    const node = findOne(target, ["switch"]);
    if (node.kind !== "switch" || !node.handler) throw new Error(`${describe(node)} is not a switch`);
    if (node.disabled) throw new Error(`${describe(node)} is disabled`);
    await reveal(node.fiber);
    await markTouch(node.fiber, pressHold(pressMs));
    clearMarker();
    const next = typeof value === "boolean" ? value : !node.checked;
    node.handler.memoizedProps.onValueChange(next);
    return next;
  },
  invoke: async ({ target, prop, args }) => {
    const node = findOne(target);
    const holder = nearestHandler(node.fiber, prop) ?? (node.handler && typeof node.handler.memoizedProps?.[prop] === "function" ? node.handler : null);
    if (!holder) throw new Error(`${describe(node)} has no ${prop} handler within reach`);
    return jsonSafe(await holder.memoizedProps[prop](...(args ?? [])));
  },
  scroll: ({ target, to = "end", offset, index, animated = false }) => {
    const origin = target ? findOne(target).fiber : focusedScreenFiber();
    const scrollable = (target && scrollableFrom(origin, false)) || scrollableFrom(origin, true);
    if (!scrollable) throw new Error(`no scrollable container around ${JSON.stringify(target ?? currentRouteName())}`);
    if (typeof index === "number" && scrollable.scrollToIndex) scrollable.scrollToIndex({ index, animated });
    else if (typeof offset === "number") {
      if (scrollable.scrollToOffset) scrollable.scrollToOffset({ offset, animated });
      else scrollable.scrollTo({ y: offset, animated });
    } else if (to === "top") {
      if (scrollable.scrollToOffset) scrollable.scrollToOffset({ offset: 0, animated });
      else scrollable.scrollTo({ y: 0, animated });
    } else scrollable.scrollToEnd({ animated });
    return true;
  },
  navigate: async ({ name, params }) => {
    if (!navigationRef.isReady()) throw new Error("navigation is not ready");
    const root = navigationRef.getRootState();
    const path = pathTo(root, name);
    if (!path) {
      throw new Error(`no mounted navigator has a route named "${name}"; reachable: ${reachableRouteNames(root).join(", ")}`);
    }
    if (path.length === 0) navigationRef.navigate(name, params);
    else {
      let nested: any = { screen: name, params };
      for (let i = path.length - 1; i > 0; i--) nested = { screen: path[i], params: nested };
      navigationRef.navigate(path[0], nested);
    }
    const start = Date.now();
    while (Date.now() - start < 1500) {
      if (focusedNames().includes(name)) return currentRouteName();
      await sleep(30);
    }
    throw new Error(`navigate("${name}") was dispatched but ${currentRouteName()} is still focused`);
  },
  goBack: () => {
    if (!navigationRef.canGoBack()) throw new Error(`cannot go back from ${currentRouteName()}`);
    navigationRef.goBack();
    return currentRouteName();
  },
  route: () => ({
    name: currentRouteName(),
    params: jsonSafe(navigationRef.getCurrentRoute()?.params ?? null),
    stack: jsonSafe(navigationRef.isReady() ? navigationRef.getRootState() : null),
  }),
  annotate: async ({ op, key, target, ...rest }) => {
    const host = overlay();
    if (!host) throw new Error("the harness overlay is not mounted");
    if (op === "remove") {
      anchors.delete(key);
      host.remove(key);
      return true;
    }
    if (op === "clear") {
      for (const item of host.list()) if (!rest.kind || item.kind === rest.kind) anchors.delete(item.key);
      host.clear(rest.kind);
      return true;
    }
    if (op === "list") return host.list();
    if (!["title", "caption", "callout", "highlight", "spotlight", "beacon"].includes(op)) throw new Error(`unknown annotation ${op}`);
    if (target !== undefined && target !== null) {
      const node = findOne(target);
      await reveal(node.fiber);
      const box = await layoutOf(node.fiber);
      anchors.set(key, target);
      host.set({ key, kind: op, box, ...rest } as any);
      trackAnchors();
    } else {
      host.set({ key, kind: op, ...rest } as any);
    }
    await nextTwoFrames();
    return key;
  },
  pointer: async ({ op, target, ms }) => {
    const host = overlay();
    if (!host) return false;
    if (op === "hide") {
      host.hidePointer();
      return true;
    }
    const node = findOne(target);
    await reveal(node.fiber);
    return glideTo(node.fiber, typeof ms === "number" ? ms : 300);
  },
  logout: async () => {
    const { logout, signedOutContainer } = harnessOptions();
    if (!logout) throw new Error("this app did not give startHarnessBridge a logout function");
    await logout();
    const start = Date.now();
    while (signedOutContainer && Date.now() - start < 8000 && navigationRef.container() !== signedOutContainer) await sleep(50);
    return navigationRef.container();
  },
  closeDevMenu: () => {
    harnessOptions().closeDevMenu?.();
    return "closed";
  },
  routeNames: () => (navigationRef.isReady() ? reachableRouteNames(navigationRef.getRootState()) : []),
  state: ({ path }) => jsonSafe(readPath(requireStore().getState(), path)),
  dispatch: ({ action }) => {
    requireStore().dispatch(action);
    return true;
  },
  patch: async ({ target, key, props, style, text }) => {
    const node = findOne(target);
    const name = key ?? `p${++patchCounter}`;
    const instance = publicInstance(node.fiber);
    const native: Record<string, unknown> = { ...(props ?? {}) };
    if (style) native.style = style;
    if (text !== undefined && node.kind === "input") native.text = String(text);
    if (instance?.setNativeProps && (node.kind !== "text" || text === undefined)) {
      instance.setNativeProps(native);
      patches.set(name, { target, mode: "native" });
      return { key: name, mode: "native" };
    }
    const host = overlay();
    if (!host) throw new Error("the harness overlay is not mounted");
    const box = await layoutOf(node.fiber);
    anchors.set(name, target);
    host.set({ key: name, kind: "patch", box, style, text: text === undefined ? undefined : String(text) } as any);
    trackAnchors();
    patches.set(name, { target, mode: "overlay" });
    return { key: name, mode: "overlay" };
  },
  unpatch: ({ key }) => {
    const entry = patches.get(key);
    patches.delete(key);
    anchors.delete(key);
    overlay()?.remove(key);
    return { key, removed: Boolean(entry), mode: entry?.mode ?? null };
  },
  inject: async ({ key, component, props, target, box, position }) => {
    const host = overlay();
    if (!host) throw new Error("the harness overlay is not mounted");
    if (!harnessOptions().components?.[component]) {
      throw new Error(`no injectable component "${component}"; registered: ${Object.keys(harnessOptions().components ?? {}).join(", ") || "none"}`);
    }
    const name = key ?? `i${++patchCounter}`;
    let frame = box ?? null;
    if (target !== undefined && target !== null) {
      const node = findOne(target);
      frame = await layoutOf(node.fiber);
      anchors.set(name, target);
      trackAnchors();
    }
    host.set({ key: name, kind: "component", component, props: props ?? {}, box: frame, position } as any);
    await nextTwoFrames();
    return name;
  },
  emit: ({ name, data }) => {
    emit(name, data);
    return true;
  },
  fakes: () => registeredFakes(),
  layout: ({ target }) => layoutOf(findOne(target).fiber),
  idle: (options) => waitIdle(options ?? {}),
  waitFor: ({ target, timeout, gone }) => waitFor(target, { timeout, gone }),
  eval: async ({ code }) => {
    const context = {
      nav: navigationRef,
      store: harnessOptions().store ?? null,
      tree: () => renderTree(buildTree(), 400),
      find: findAll,
      Platform,
      Dimensions,
      captureClean,
    };
    const fn = new Function("ctx", `const { nav, store, tree, find, Platform, Dimensions, captureClean } = ctx; ${code}`);
    return jsonSafe(await Promise.resolve(fn(context)));
  },
};

async function execute(command: { id: string; cmd: string; args?: any }) {
  const handler = commands[command.cmd];
  if (!handler) return { id: command.id, ok: false, error: `unknown bridge command ${command.cmd}` };
  try {
    const value = await handler(command.args ?? {});
    return { id: command.id, ok: true, value: jsonSafe(value) };
  } catch (error: any) {
    clearMarker();
    return { id: command.id, ok: false, error: error?.message ?? String(error) };
  }
}

function trackNetwork() {
  const proto = (globalThis as any).XMLHttpRequest?.prototype;
  if (!proto || proto.__ehxTracked) return;
  proto.__ehxTracked = true;
  const open = proto.open;
  const send = proto.send;
  proto.open = function (method: string, url: string, ...rest: unknown[]) {
    this.__ehxIgnored =
      typeof url === "string" &&
      (url.startsWith(harnessUrl()) || url.includes("/symbolicate") || url.includes(":8081/") || ignoredUrl(url));
    return open.call(this, method, url, ...rest);
  };
  proto.send = function (...args: unknown[]) {
    if (!this.__ehxIgnored) {
      inflight++;
      lastNetwork = Date.now();
      const done = () => {
        inflight = Math.max(0, inflight - 1);
        lastNetwork = Date.now();
        this.removeEventListener?.("loadend", done);
      };
      this.addEventListener?.("loadend", done);
    }
    return send.apply(this, args);
  };
}

function trackCommits() {
  const hook = (globalThis as any).__REACT_DEVTOOLS_GLOBAL_HOOK__;
  if (!hook || hook.__ehxTracked) return;
  hook.__ehxTracked = true;
  const original = hook.onCommitFiberRoot;
  hook.onCommitFiberRoot = function (...args: unknown[]) {
    lastCommit = Date.now();
    return original?.apply(this, args);
  };
}

function routeNativeAlerts() {
  const showAlert = harnessOptions().alert;
  if (!showAlert) return;
  const native = Alert.alert;
  if ((native as any).__ehxRouted) return;
  const routed = (title: string, message?: string, buttons?: any[], options?: any) => {
    const list = buttons?.length ? buttons : [{ text: "OK" }];
    showAlert({
      title,
      message,
      dismissable: options?.cancelable ?? true,
      actions: list.map((button: any) => ({
        text: button.text ?? "OK",
        cancel: button.style === "cancel",
        destructive: button.style === "destructive",
        onPress: button.onPress ? () => button.onPress() : undefined,
      })),
    });
  };
  (routed as any).__ehxRouted = true;
  Alert.alert = routed as typeof Alert.alert;
}

function reply(message: Record<string, unknown>) {
  const binding = (globalThis as any)[REPLY_BINDING];
  if (typeof binding !== "function") return;
  let payload: string;
  try {
    payload = JSON.stringify(message);
  } catch (error: any) {
    payload = JSON.stringify({ type: message.type, id: message.id, ok: false, error: `unserializable result: ${error?.message ?? error}` });
  }
  binding(payload);
}

function dispatch(raw: string) {
  const command = JSON.parse(raw);
  void execute(command).then((result) => reply({ type: "result", ...result }));
  return true;
}

export function startHarnessBridge(options: HarnessOptions = {}) {
  configure(options);
  if (!harnessEnabled() || started) return;
  started = true;
  trackNetwork();
  trackCommits();
  routeNativeAlerts();
  captureClean();
  const screen = Dimensions.get("window");
  const info = {
    platform: Platform.OS,
    session: `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`,
    osVersion: String(Platform.Version),
    window: { width: screen.width, height: screen.height },
    pixelRatio: PixelRatio.get(),
    fakes: registeredFakes(),
    commands: Object.keys(commands),
  };
  (globalThis as any)[BRIDGE_GLOBAL] = { hello: () => ({ ...info, fakes: registeredFakes() }), dispatch };
  reply({ type: "hello", info });
}

let patchCounter = 0;
const patches = new Map<string, { target: Target; mode: "native" | "overlay" }>();

function requireStore() {
  const store = harnessOptions().store;
  if (!store) throw new Error("this app did not give startHarnessBridge a store");
  return store;
}

function ignoredUrl(url: string) {
  return (harnessOptions().ignoreUrls ?? []).some((pattern) => (typeof pattern === "string" ? url.includes(pattern) : pattern.test(url)));
}

export function emit(name: string, data?: unknown) {
  if (!harnessEnabled()) return;
  if (typeof (globalThis as any)[REPLY_BINDING] === "function") reply({ type: "event", name, data: jsonSafe(data ?? null) });
  else harnessReport(name, data);
}
