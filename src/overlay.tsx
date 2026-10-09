import React from "react";
import { Animated, Easing, StyleSheet, Text, View } from "react-native";
import { harnessOptions, themeOf } from "./config";
import type { Box } from "./geometry";

const CAPTION_CLEARANCE = 112;
const POINTER_IDLE_MS = 1800;

export type OverlayItem =
  | { key: string; kind: "title"; title: string; subtitle?: string; kicker?: string }
  | { key: string; kind: "caption"; text: string; step?: string | number; position?: "top" | "bottom" }
  | { key: string; kind: "callout"; text: string; box: Box | null; placement?: "auto" | "top" | "bottom" }
  | { key: string; kind: "highlight"; box: Box | null }
  | { key: string; kind: "spotlight"; box: Box | null }
  | { key: string; kind: "beacon"; color: string }
  | { key: string; kind: "patch"; box: Box | null; style?: Record<string, unknown>; text?: string }
  | { key: string; kind: "component"; component: string; props?: Record<string, unknown>; box: Box | null; position?: Record<string, number> };

type Size = { width: number; height: number };

type State = {
  items: OverlayItem[];
  size: Size;
  pointerShown: boolean;
  ripples: { key: number; x: number; y: number }[];
};

export type OverlayHost = {
  set: (item: OverlayItem) => void;
  move: (key: string, box: Box | null) => void;
  remove: (key: string) => void;
  clear: (kind?: string) => void;
  list: () => { key: string; kind: string; anchored: boolean; visible: boolean }[];
  glide: (x: number, y: number, ms: number) => Promise<void>;
  tap: (x: number, y: number) => void;
  hidePointer: () => void;
};

let overlayHost: OverlayHost | null = null;

export function overlay(): OverlayHost | null {
  return overlayHost;
}

function FadeIn({ children, style }: { children?: React.ReactNode; style?: any }) {
  const opacity = React.useRef(new Animated.Value(0)).current;
  React.useEffect(() => {
    Animated.timing(opacity, { toValue: 1, duration: 220, useNativeDriver: true }).start();
  }, [opacity]);
  return (
    <Animated.View pointerEvents="none" style={[style, { opacity }]}>
      {children}
    </Animated.View>
  );
}

function Ripple({ x, y, onDone }: { x: number; y: number; onDone: () => void }) {
  const progress = React.useRef(new Animated.Value(0)).current;
  const done = React.useRef(onDone);
  done.current = onDone;
  React.useEffect(() => {
    Animated.timing(progress, { toValue: 1, duration: 600, easing: Easing.out(Easing.quad), useNativeDriver: true }).start(() =>
      done.current(),
    );
  }, [progress]);
  const scale = progress.interpolate({ inputRange: [0, 1], outputRange: [0.4, 1.6] });
  const opacity = progress.interpolate({ inputRange: [0, 0.6, 1], outputRange: [1, 0.8, 0] });
  return <Animated.View pointerEvents="none" style={[styles.ripple, { left: x - 32, top: y - 32, opacity, transform: [{ scale }] }]} />;
}

const BUBBLE_ROOM = 96;

function bubblePosition(box: Box, size: Size, placement: string | undefined) {
  const roomAbove = box.y - 16 > BUBBLE_ROOM + 44;
  const roomBelow = size.height - (box.y + box.height + 16) > BUBBLE_ROOM + CAPTION_CLEARANCE;
  const side = placement === "top" || placement === "bottom" ? placement : roomAbove ? "top" : roomBelow ? "bottom" : "top";
  return side === "bottom" ? { top: box.y + box.height + 16 } : { bottom: Math.max(12, size.height - box.y + 16) };
}

export class HarnessOverlay extends React.Component<object, State> {
  state: State = { items: [], size: { width: 0, height: 0 }, pointerShown: false, ripples: [] };

  private pointer = new Animated.ValueXY({ x: 0, y: 0 });

  private pointerOpacity = new Animated.Value(0);

  private at: { x: number; y: number } | null = null;

  private rippleKey = 0;

  private idleTimer: ReturnType<typeof setTimeout> | null = null;

  private scheduleIdle() {
    if (this.idleTimer) clearTimeout(this.idleTimer);
    this.idleTimer = setTimeout(() => {
      Animated.timing(this.pointerOpacity, { toValue: 0, duration: 250, useNativeDriver: true }).start();
    }, POINTER_IDLE_MS);
  }

  componentDidMount() {
    overlayHost = {
      set: (item) => this.setState(({ items }) => ({ items: [...items.filter((i) => i.key !== item.key), item] })),
      move: (key, box) =>
        this.setState(({ items }) => ({
          items: items.map((i) => (i.key === key && "box" in i ? ({ ...i, box } as OverlayItem) : i)),
        })),
      remove: (key) => this.setState(({ items }) => ({ items: items.filter((i) => i.key !== key) })),
      clear: (kind) => this.setState(({ items }) => ({ items: kind ? items.filter((i) => i.kind !== kind) : [] })),
      list: () =>
        this.state.items.map((i) => ({ key: i.key, kind: i.kind, anchored: "box" in i, visible: !("box" in i) || Boolean(i.box) })),
      glide: (x, y, ms) =>
        new Promise<void>((resolve) => {
          const from = this.at ?? { x: this.state.size.width / 2, y: this.state.size.height * 0.75 };
          this.at = { x, y };
          const move = () =>
            Animated.parallel([
              Animated.timing(this.pointerOpacity, { toValue: 1, duration: 120, useNativeDriver: true }),
              Animated.timing(this.pointer, {
                toValue: { x, y },
                duration: Math.max(0, ms),
                easing: Easing.inOut(Easing.cubic),
                useNativeDriver: true,
              }),
            ]).start(() => {
              this.scheduleIdle();
              resolve();
            });
          if (this.state.pointerShown) {
            move();
            return;
          }
          this.pointer.setValue(from);
          this.setState({ pointerShown: true }, () => requestAnimationFrame(move));
        }),
      tap: (x, y) => this.setState(({ ripples }) => ({ ripples: [...ripples, { key: ++this.rippleKey, x, y }] })),
      hidePointer: () => {
        this.at = null;
        Animated.timing(this.pointerOpacity, { toValue: 0, duration: 150, useNativeDriver: true }).start();
      },
    };
  }

  componentWillUnmount() {
    if (this.idleTimer) clearTimeout(this.idleTimer);
    overlayHost = null;
  }

  private renderItem(item: OverlayItem) {
    const { size } = this.state;
    switch (item.kind) {
      case "title":
        return (
          <View key={item.key} pointerEvents="none" style={styles.titleCard}>
            {item.kicker ? <Text style={styles.kicker}>{item.kicker.toLocaleUpperCase(harnessOptions().locale)}</Text> : null}
            <Text style={styles.title}>{item.title}</Text>
            <View style={styles.rule} />
            {item.subtitle ? <Text style={styles.subtitle}>{item.subtitle}</Text> : null}
          </View>
        );
      case "caption":
        return (
          <FadeIn key={item.key} style={[styles.captionWrap, item.position === "top" ? { top: 64 } : { bottom: CAPTION_CLEARANCE }]}>
            <View style={styles.caption}>
              {item.step !== undefined && item.step !== null ? (
                <View style={styles.badge}>
                  <Text style={styles.badgeText}>{String(item.step)}</Text>
                </View>
              ) : null}
              <Text style={styles.captionText}>{item.text}</Text>
            </View>
          </FadeIn>
        );
      case "highlight":
        if (!item.box) return null;
        return <FadeIn key={item.key} style={[styles.ring, ringFrame(item.box)]} />;
      case "spotlight": {
        if (!item.box) return null;
        const b = item.box;
        const pad = 8;
        const top = Math.max(0, b.y - pad);
        const bottom = b.y + b.height + pad;
        const left = Math.max(0, b.x - pad);
        const right = b.x + b.width + pad;
        return (
          <FadeIn key={item.key} style={StyleSheet.absoluteFill}>
            <View style={[styles.dim, { left: 0, right: 0, top: 0, height: top }]} />
            <View style={[styles.dim, { left: 0, right: 0, top: bottom, bottom: 0 }]} />
            <View style={[styles.dim, { left: 0, width: left, top, height: bottom - top }]} />
            <View style={[styles.dim, { left: right, right: 0, top, height: bottom - top }]} />
            <View style={[styles.ring, ringFrame(b)]} />
          </FadeIn>
        );
      }
      case "beacon":
        return <View key={item.key} pointerEvents="none" style={[styles.beacon, { backgroundColor: item.color }]} />;
      case "patch":
        if (!item.box) return null;
        return (
          <View key={item.key} pointerEvents="none" style={[{ position: "absolute", left: item.box.x, top: item.box.y, width: item.box.width, height: item.box.height, justifyContent: "center" }, item.style as any]}>
            {item.text !== undefined ? <Text style={(item.style as any)?.color ? { color: (item.style as any).color } : undefined}>{item.text}</Text> : null}
          </View>
        );
      case "component": {
        const Component = harnessOptions().components?.[item.component];
        if (!Component) return null;
        const frame = item.box
          ? { left: item.box.x, top: item.box.y, width: item.box.width, height: item.box.height }
          : item.position ?? StyleSheet.absoluteFillObject;
        return (
          <View key={item.key} pointerEvents="none" style={[{ position: "absolute" }, frame as any]}>
            <Component {...(item.props ?? {})} box={item.box} />
          </View>
        );
      }
      case "callout":
        if (!item.box) return null;
        return (
          <FadeIn key={item.key} style={StyleSheet.absoluteFill}>
            <View style={[styles.ring, ringFrame(item.box)]} />
            <View style={[styles.bubbleWrap, bubblePosition(item.box, size, item.placement)]}>
              <View style={styles.bubble}>
                <Text style={styles.bubbleText}>{item.text}</Text>
              </View>
            </View>
          </FadeIn>
        );
    }
  }

  render() {
    const { items, pointerShown, ripples } = this.state;
    return (
      <View
        pointerEvents="none"
        style={StyleSheet.absoluteFill}
        onLayout={(e) => this.setState({ size: { width: e.nativeEvent.layout.width, height: e.nativeEvent.layout.height } })}
      >
        {items.filter((i) => i.kind !== "title" && i.kind !== "beacon").map((item) => this.renderItem(item))}
        {ripples.map((r) => (
          <Ripple
            key={r.key}
            x={r.x}
            y={r.y}
            onDone={() => this.setState(({ ripples: list }) => ({ ripples: list.filter((x) => x.key !== r.key) }))}
          />
        ))}
        {pointerShown ? (
          <Animated.View
            pointerEvents="none"
            style={[
              styles.finger,
              {
                opacity: this.pointerOpacity,
                transform: [{ translateX: Animated.add(this.pointer.x, -17) }, { translateY: Animated.add(this.pointer.y, -17) }],
              },
            ]}
          />
        ) : null}
        {items.filter((i) => i.kind === "title").map((item) => this.renderItem(item))}
        {items.filter((i) => i.kind === "beacon").map((item) => this.renderItem(item))}
      </View>
    );
  }
}

function ringFrame(box: Box) {
  return { left: box.x - 6, top: box.y - 6, width: box.width + 12, height: box.height + 12 };
}

const styles = new Proxy({} as Record<string, any>, {
  get: (_, key: string) => stylesFor()[key],
});

let cachedTheme: unknown = null;
let cachedStyles: Record<string, any> = {};

function stylesFor() {
  const t = themeOf();
  if (cachedTheme === t) return cachedStyles;
  cachedTheme = t;
  cachedStyles = StyleSheet.create({
    beacon: { position: "absolute", left: 1, top: 1, width: 14, height: 14 },
    titleCard: { ...StyleSheet.absoluteFillObject, backgroundColor: t.surfaceDeep, alignItems: "center", justifyContent: "center", paddingHorizontal: 32 },
    kicker: { color: t.accentLight, fontSize: 13, fontWeight: "700", letterSpacing: 3, marginBottom: 14, fontFamily: t.fontFamily },
    title: { color: t.onSurface, fontSize: 34, fontWeight: "800", textAlign: "center", letterSpacing: -0.8, fontFamily: t.fontFamily },
    rule: { width: 72, height: 4, borderRadius: 2, backgroundColor: t.accent, marginVertical: 18 },
    subtitle: { color: t.onSurfaceMuted, fontSize: 17, fontWeight: "500", textAlign: "center", lineHeight: 24, fontFamily: t.fontFamily },
    captionWrap: { position: "absolute", left: 12, right: 12, alignItems: "stretch" },
    caption: {
      flexDirection: "row", alignItems: "center", backgroundColor: t.captionBackground, borderColor: t.captionBorder,
      borderWidth: 1, borderRadius: t.radius, paddingVertical: 12, paddingHorizontal: 16,
    },
    badge: { minWidth: 28, height: 28, borderRadius: 14, backgroundColor: t.accentLight, alignItems: "center", justifyContent: "center", marginRight: 10, paddingHorizontal: 6 },
    badgeText: { color: t.surfaceDeep, fontWeight: "800", fontSize: 14, fontFamily: t.fontFamily },
    captionText: { color: t.onSurface, fontSize: 15, fontWeight: "600", lineHeight: 20, flexShrink: 1, fontFamily: t.fontFamily },
    ring: { position: "absolute", borderWidth: 3, borderColor: t.accentLight, borderRadius: t.radius, backgroundColor: t.ringFill },
    dim: { position: "absolute", backgroundColor: t.scrim },
    bubbleWrap: { position: "absolute", left: 16, right: 16, alignItems: "center" },
    bubble: { backgroundColor: t.surface, borderColor: t.accentLight, borderWidth: 2, borderRadius: t.radius, paddingVertical: 10, paddingHorizontal: 14, maxWidth: 320 },
    bubbleText: { color: t.onSurface, fontSize: 15, fontWeight: "600", lineHeight: 20, fontFamily: t.fontFamily },
    finger: {
      position: "absolute", left: 0, top: 0, width: 34, height: 34, borderRadius: 17, backgroundColor: t.pointerFill,
      borderColor: t.pointerBorder, borderWidth: 3, elevation: 6,
    },
    ripple: { position: "absolute", width: 64, height: 64, borderRadius: 32, borderWidth: 4, borderColor: t.accentLight, backgroundColor: t.ringFill },
  });
  return cachedStyles;
}
