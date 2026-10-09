import http from 'node:http'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { createRequire } from 'node:module'
import { spawnSync } from 'node:child_process'
import { pathToFileURL } from 'node:url'
import { createRunner, renderText } from '../../kit/src/script-runner.mjs'
import { chromeEndpoint, heartbeatSids, leaseReleased, request as daemonRequest, updateLeaseMeta, uploadArtifact } from '../../kit/src/daemon-client.mjs'
import { loadWorkerConfig, resolveTheme } from '../../kit/src/config.mjs'
import { loadExtensions } from '../../kit/src/extensions.mjs'

const CONFIG = loadWorkerConfig()
const PORT = Number(CONFIG.port ?? 0)
const WEB_BASE_URL = String(CONFIG.baseUrl || 'http://localhost:3000').replace(/\/+$/, '')
const ARTIFACTS_DIR = CONFIG.artifactsDir || path.join(os.tmpdir(), 'eks-harness-web')
const STATE_DIR = CONFIG.stateDir || path.resolve('.ehx-web-state')
const LEASE_SID = CONFIG.sid || process.env.EHX_LEASE_SID || null
const LOCALE = CONFIG.locale || 'en-US'
const TIMEZONE = CONFIG.timezone || undefined
const HOME_PATH = CONFIG.home || '/'
const API_BASE_URL = CONFIG.apiBaseUrl ? String(CONFIG.apiBaseUrl).replace(/\/+$/, '') : null
const THEME = resolveTheme(CONFIG.theme, LOCALE)
const opt = (name, fallback) => (CONFIG.options && CONFIG.options[name] !== undefined ? CONFIG.options[name] : fallback)

const DEFAULT_VIEWPORTS = {
  desktop: { width: 1920, height: 1080, deviceScaleFactor: 1, isMobile: false, hasTouch: false },
  mobile: {
    width: 393, height: 852, deviceScaleFactor: 3, isMobile: true, hasTouch: true,
    userAgent: 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1',
  },
}

function viewportProfile(name) {
  const spec = { ...DEFAULT_VIEWPORTS[name], ...((CONFIG.viewports ?? {})[name] ?? {}) }
  const { width, height, label, ...rest } = spec
  return { label: label || `${name}-${width}x${height}`, viewport: { width: Number(width), height: Number(height) }, ...rest }
}

const VIEWPORTS = { desktop: viewportProfile('desktop'), mobile: viewportProfile('mobile') }

const POINTER_OVERLAY = `(() => {
  const T = ${JSON.stringify(THEME)};
  if (window.__ehxPointer) return;
  window.__ehxPointer = true;
  let mark = null;
  let dot = null;
  let at = null;
  const move = (x, y) => {
    at = { x, y };
    if (!dot) return;
    dot.style.opacity = '1';
    dot.style.transform = 'translate(' + x + 'px,' + y + 'px)';
  };
  window.__ehxPointerTo = (x, y, ms) => new Promise((resolve) => {
    const from = at || { x, y };
    const started = performance.now();
    const step = (now) => {
      const t = ms > 0 ? Math.min(1, (now - started) / ms) : 1;
      move(from.x + (x - from.x) * t, from.y + (y - from.y) * t);
      if (t < 1) requestAnimationFrame(step);
      else resolve(true);
    };
    requestAnimationFrame(step);
  });
  const hide = () => { if (mark) mark.style.display = 'none'; };
  window.__ehxMark = {
    show: (x, y, w, h) => {
      if (!mark) return false;
      mark.style.left = (x - 4) + 'px';
      mark.style.top = (y - 4) + 'px';
      mark.style.width = (w + 8) + 'px';
      mark.style.height = (h + 8) + 'px';
      mark.style.display = 'block';
      return true;
    },
    hide,
    visible: () => Boolean(mark && mark.style.display === 'block'),
  };
  const install = () => {
    dot = document.createElement('div');
    dot.id = 'ehx-pointer-dot';
    dot.setAttribute('data-ehx-overlay', 'pointer');
    dot.style.cssText = [
      'position:fixed','z-index:2147483647','pointer-events:none','left:0','top:0',
      'width:26px','height:32px','margin:-2px 0 0 -3px','filter:drop-shadow(0 2px 3px rgba(0,0,0,.45))',
      'transition:transform .04s linear','opacity:0',
    ].join(';');
    dot.innerHTML = '<svg width="26" height="32" viewBox="0 0 26 32"><path d="M3 2 L3 26 L9.5 20 L14 30 L18 28 L13.5 18.5 L22 18.5 Z" fill="#fff" stroke="' + T.pointerStroke + '" stroke-width="2" stroke-linejoin="round"/></svg>';
    document.documentElement.appendChild(dot);
    mark = document.createElement('div');
    mark.id = 'ehx-press-mark';
    mark.style.cssText = [
      'position:fixed','z-index:2147483646','pointer-events:none','display:none','box-sizing:border-box',
      'border:3px solid rgba(' + T.accentRgb + ',.95)','border-radius:10px','background:rgba(' + T.accentRgb + ',.14)',
    ].join(';');
    document.documentElement.appendChild(mark);

    if (at) move(at.x, at.y);
    addEventListener('mousemove', (e) => move(e.clientX, e.clientY), true);
    addEventListener('mousedown', (e) => move(e.clientX, e.clientY), true);
    addEventListener('touchstart', (e) => {
      const t = e.changedTouches[0];
      if (t) move(t.clientX, t.clientY);
    }, true);
    for (const type of ['pointerdown', 'mousedown', 'touchstart', 'keydown', 'beforeinput', 'input', 'wheel', 'click']) {
      addEventListener(type, hide, true);
    }
  };
  if (document.documentElement) install();
  else addEventListener('DOMContentLoaded', install);
})()`
const ANNOTATION_OVERLAY = `(() => {
  const T = ${JSON.stringify(THEME)};
  if (window.__ehxAnno) return;
  const NAVY = T.surface, NAVY_DEEP = T.surfaceDeep, GOLD = T.accent, GOLD_LIGHT = T.accentLight;
  const FONT = T.font;
  const items = new Map();
  let layer = null;
  const ensure = () => {
    if (layer && layer.isConnected) return layer;
    layer = document.createElement('div');
    layer.setAttribute('data-ehx-overlay', 'annotations');
    layer.style.cssText = 'position:fixed;left:0;top:0;width:100vw;height:var(--ehx-vh,100vh);overflow:hidden;z-index:2147483600;pointer-events:none;font-family:' + FONT;
    document.documentElement.appendChild(layer);
    return layer;
  };
  const node = (css, html) => {
    const el = document.createElement('div');
    el.setAttribute('data-ehx-overlay', 'item');
    el.style.cssText = css;
    if (html !== undefined) el.innerHTML = html;
    return el;
  };
  const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const fadeIn = (el) => { el.style.opacity = '0'; el.style.transition = 'opacity .25s ease, transform .25s ease'; requestAnimationFrame(() => { el.style.opacity = '1'; }); };
  const remove = (key) => {
    const item = items.get(key);
    if (!item) return false;
    items.delete(key);
    for (const el of item.els) {
      el.style.transition = 'opacity .2s ease';
      el.style.opacity = '0';
      setTimeout(() => el.remove(), 220);
    }
    return true;
  };
  const add = (key, kind, els, anchor, place, find) => {
    remove(key);
    const root = ensure();
    for (const el of els) { root.appendChild(el); fadeIn(el); }
    items.set(key, { kind, els, anchor, place, find });
    if (anchor) place();
    return key;
  };
  const target = (key) => document.querySelector('[data-ehx-anno~="' + key + '"]');
  const findBy = (find) => {
    if (!find) return null;
    if (find.selector) {
      try { const el = [...document.querySelectorAll(find.selector)].find((c) => c.getClientRects().length); if (el) return el; } catch {}
    }
    if (find.text) {
      const want = String(find.text).toLocaleLowerCase(T.locale);
      let best = null;
      for (const el of document.querySelectorAll('body *')) {
        if (el.closest('[data-ehx-overlay]') || !el.getClientRects().length) continue;
        const own = (el.innerText || '').trim().toLocaleLowerCase(T.locale);
        if (own && own.includes(want) && (!best || own.length < (best.innerText || '').length)) best = el;
      }
      return best;
    }
    return null;
  };
  const rectOf = (key, find) => {
    let el = target(key);
    if (!el && find) {
      el = findBy(find);
      if (el) el.setAttribute('data-ehx-anno', ((el.getAttribute('data-ehx-anno') || '') + ' ' + key).trim());
    }
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return r.width || r.height ? r : null;
  };
  const tick = () => {
    for (const item of items.values()) if (item.anchor) item.place();
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
  const api = {
    title({ key = 'title', title, subtitle, kicker }) {
      const card = node('position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:18px;'
        + 'background:' + NAVY_DEEP + ';color:' + T.onSurface + ';text-align:center;padding:40px',
        (kicker ? '<div style="font-size:18px;letter-spacing:.28em;text-transform:uppercase;color:' + GOLD_LIGHT + ';font-weight:700">' + esc(kicker) + '</div>' : '')
        + '<div style="font-size:clamp(34px,5vw,68px);font-weight:800;letter-spacing:-.035em;line-height:1.05;max-width:80vw">' + esc(title) + '</div>'
        + '<div style="width:96px;height:4px;border-radius:2px;background:' + GOLD_LIGHT + '"></div>'
        + (subtitle ? '<div style="font-size:clamp(16px,1.8vw,26px);font-weight:500;color:rgba(255,255,255,.82);max-width:70vw;line-height:1.35">' + esc(subtitle) + '</div>' : ''));
      return add(key, 'title', [card]);
    },
    caption({ key = 'caption', text, step, position = 'bottom' }) {
      const where = position === 'top' ? 'top:28px' : 'bottom:36px';
      const badge = step !== undefined && step !== null
        ? '<span style="flex:none;display:inline-flex;align-items:center;justify-content:center;min-width:34px;height:34px;padding:0 8px;border-radius:17px;background:' + GOLD_LIGHT + ';color:' + NAVY_DEEP + ';font-weight:800;font-size:17px">' + esc(step) + '</span>'
        : '';
      const bar = node('position:absolute;left:50%;' + where + ';transform:translateX(-50%);display:flex;align-items:center;gap:14px;max-width:min(1100px,92vw);'
        + 'padding:14px 26px 14px ' + (badge ? '14px' : '26px') + ';border-radius:16px;background:rgba(' + T.surfaceRgb + ',.97);color:' + T.onSurface + ';'
        + 'box-shadow:0 12px 40px rgba(0,0,0,.35);border:1px solid rgba(' + T.accentRgb + ',.45);font-size:clamp(15px,1.35vw,22px);font-weight:600;line-height:1.35',
        badge + '<span>' + esc(text) + '</span>');
      return add(key, 'caption', [bar]);
    },
    callout({ key, text, placement = 'auto', find }) {
      const bubble = node('position:fixed;left:0;top:0;max-width:380px;padding:12px 18px;border-radius:12px;background:' + NAVY + ';color:' + T.onSurface + ';'
        + 'font-size:17px;font-weight:600;line-height:1.4;box-shadow:0 12px 34px rgba(0,0,0,.35);border:2px solid ' + GOLD_LIGHT, esc(text));
      const ring = node('position:fixed;left:0;top:0;border:3px solid ' + GOLD_LIGHT + ';border-radius:10px;box-shadow:0 0 0 4px rgba(' + T.accentRgb + ',.25)');
      const arrow = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      arrow.setAttribute('data-ehx-overlay', 'item');
      arrow.style.cssText = 'position:fixed;inset:0;width:100vw;height:100vh;overflow:visible;pointer-events:none';
      arrow.innerHTML = '<path fill="none" stroke="' + GOLD_LIGHT + '" stroke-width="3" stroke-linecap="round"></path><path fill="' + GOLD_LIGHT + '"></path>';
      const place = () => {
        const r = rectOf(key, find);
        const shown = Boolean(r);
        for (const el of [bubble, ring, arrow]) el.style.visibility = shown ? 'visible' : 'hidden';
        if (!r) return;
        ring.style.transform = 'translate(' + (r.left - 6) + 'px,' + (r.top - 6) + 'px)';
        ring.style.width = (r.width + 12) + 'px';
        ring.style.height = (r.height + 12) + 'px';
        const bw = bubble.offsetWidth, bh = bubble.offsetHeight, vw = innerWidth, vh = Math.min(innerHeight, ensure().clientHeight || innerHeight), gap = 34, edge = 24, captionZone = 120;
        const big = r.width > vw * 0.6 || r.height > vh * 0.6;
        let side = placement;
        if (side === 'auto') {
          if (big) side = 'inside';
          else if (r.right + gap + bw + edge < vw) side = 'right';
          else if (r.left - gap - bw - edge > 0) side = 'left';
          else if (r.bottom + gap + bh + edge < vh - captionZone) side = 'bottom';
          else side = 'top';
        }
        let x, y;
        const cy = r.top + r.height / 2, cx = r.left + r.width / 2;
        if (side === 'right') { x = r.right + gap; y = cy - bh / 2; }
        else if (side === 'left') { x = r.left - gap - bw; y = cy - bh / 2; }
        else if (side === 'bottom') { x = cx - bw / 2; y = r.bottom + gap; }
        else if (side === 'top') { x = cx - bw / 2; y = r.top - gap - bh; }
        else { x = Math.min(innerWidth, r.right) - bw - 48; y = Math.max(r.top, 0) + 48; }
        x = Math.max(edge, Math.min(vw - bw - edge, x));
        y = Math.max(edge, Math.min(vh - bh - edge, y));
        bubble.style.transform = 'translate(' + x + 'px,' + y + 'px)';
        const [line, head] = arrow.children;
        if (side === 'inside') { line.setAttribute('d', ''); head.setAttribute('d', ''); return; }
        let sx, sy, ex, ey;
        if (side === 'right') { sx = x; sy = y + bh / 2; ex = r.right + 8; ey = Math.max(r.top, Math.min(r.bottom, sy)); }
        else if (side === 'left') { sx = x + bw; sy = y + bh / 2; ex = r.left - 8; ey = Math.max(r.top, Math.min(r.bottom, sy)); }
        else if (side === 'bottom') { sx = x + bw / 2; sy = y; ex = Math.max(r.left, Math.min(r.right, sx)); ey = r.bottom + 8; }
        else { sx = x + bw / 2; sy = y + bh; ex = Math.max(r.left, Math.min(r.right, sx)); ey = r.top - 8; }
        const angle = Math.atan2(ey - sy, ex - sx), size = 11;
        line.setAttribute('d', 'M' + sx + ' ' + sy + ' L' + (ex - Math.cos(angle) * size * 0.8) + ' ' + (ey - Math.sin(angle) * size * 0.8));
        const lx = ex - Math.cos(angle) * size - Math.sin(angle) * size * 0.6, ly = ey - Math.sin(angle) * size + Math.cos(angle) * size * 0.6;
        const rx = ex - Math.cos(angle) * size + Math.sin(angle) * size * 0.6, ry = ey - Math.sin(angle) * size - Math.cos(angle) * size * 0.6;
        head.setAttribute('d', 'M' + ex + ' ' + ey + ' L' + lx + ' ' + ly + ' L' + rx + ' ' + ry + ' Z');
      };
      return add(key, 'callout', [ring, arrow, bubble], true, place, find);
    },
    highlight({ key, find }) {
      const ring = node('position:fixed;left:0;top:0;border:3px solid ' + GOLD_LIGHT + ';border-radius:10px;box-shadow:0 0 0 6px rgba(' + T.accentRgb + ',.22),0 0 24px rgba(' + T.accentRgb + ',.45)');
      const place = () => {
        const r = rectOf(key, find);
        ring.style.visibility = r ? 'visible' : 'hidden';
        if (!r) return;
        ring.style.transform = 'translate(' + (r.left - 6) + 'px,' + (r.top - 6) + 'px)';
        ring.style.width = (r.width + 12) + 'px';
        ring.style.height = (r.height + 12) + 'px';
      };
      return add(key, 'highlight', [ring], true, place, find);
    },
    spotlight({ key, find }) {
      const hole = node('position:fixed;left:0;top:0;border-radius:12px;box-shadow:0 0 0 200vmax rgba(' + T.surfaceRgb + ',.62);outline:3px solid ' + GOLD_LIGHT);
      const place = () => {
        const r = rectOf(key, find);
        hole.style.visibility = r ? 'visible' : 'hidden';
        if (!r) return;
        const covers = (r.width * r.height) / (innerWidth * innerHeight) > 0.6;
        const inset = covers ? 14 : -8;
        hole.style.boxShadow = covers ? 'inset 0 0 0 3px ' + GOLD_LIGHT + ',inset 0 0 28px rgba(' + T.accentRgb + ',.45),0 0 0 200vmax rgba(' + T.surfaceRgb + ',.28)' : '0 0 0 200vmax rgba(' + T.surfaceRgb + ',.62)';
        hole.style.outline = covers ? 'none' : '3px solid ' + GOLD_LIGHT;
        hole.style.transform = 'translate(' + (Math.max(0, r.left) + inset) + 'px,' + (Math.max(0, r.top) + inset) + 'px)';
        hole.style.width = (Math.min(innerWidth, r.right) - Math.max(0, r.left) - inset * 2) + 'px';
        hole.style.height = (Math.min(innerHeight, r.bottom) - Math.max(0, r.top) - inset * 2) + 'px';
      };
      return add(key, 'spotlight', [hole], true, place, find);
    },
    remove,
    clear(kind) {
      for (const [key, item] of [...items]) if (!kind || item.kind === kind) remove(key);
      return true;
    },
    list: () => [...items].map(([key, item]) => ({ key, kind: item.kind, anchored: Boolean(item.anchor), visible: item.anchor ? Boolean(rectOf(key, item.find)) : true })),
  };
  window.__ehxAnno = api;
  const ripple = (x, y) => {
    const root = ensure();
    const dot = node('position:fixed;left:' + (x - 22) + 'px;top:' + (y - 22) + 'px;width:44px;height:44px;border-radius:50%;'
      + 'border:3px solid rgba(' + T.accentRgb + ',.95);background:rgba(' + T.accentRgb + ',.18);transform:scale(.35);opacity:1;transition:transform .45s ease-out,opacity .45s ease-out');
    root.appendChild(dot);
    requestAnimationFrame(() => { dot.style.transform = 'scale(1.35)'; dot.style.opacity = '0'; });
    setTimeout(() => dot.remove(), 520);
  };
  addEventListener('pointerdown', (e) => { if (window.__ehxRipple) ripple(e.clientX, e.clientY); }, true);
})()`
const NETWORK_TRACKER = `(() => {
  const IGNORE = new RegExp(${JSON.stringify(CONFIG.networkIgnore || '$^')});
  if (window.__ehxNet) return;
  const net = window.__ehxNet = { inflight: 0, last: Date.now(), mutated: Date.now() };
  const ours = (node) => {
    const el = node && node.nodeType === 1 ? node : node && node.parentElement;
    return Boolean(el && el.closest && el.closest('[data-ehx-overlay]'));
  };
  const watch = () => new MutationObserver((records) => {
    if (records.some((record) => !ours(record.target))) net.mutated = Date.now();
  }).observe(document.documentElement, { childList: true, subtree: true, characterData: true });
  if (document.documentElement) watch(); else addEventListener('DOMContentLoaded', watch);
  const ignored = (url) => IGNORE.test(String(url || ''));
  const done = () => { net.inflight = Math.max(0, net.inflight - 1); net.last = Date.now(); };
  const originalFetch = window.fetch;
  window.fetch = function (input, init) {
    const url = typeof input === 'string' ? input : (input && input.url);
    if (ignored(url)) return originalFetch.apply(this, arguments);
    net.inflight++; net.last = Date.now();
    return originalFetch.apply(this, arguments).then((response) => { done(); return response; }, (error) => { done(); throw error; });
  };
  const open = XMLHttpRequest.prototype.open;
  const send = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url) { this.__ehxIgnored = ignored(url); return open.apply(this, arguments); };
  XMLHttpRequest.prototype.send = function () {
    if (!this.__ehxIgnored) {
      net.inflight++; net.last = Date.now();
      this.addEventListener('loadend', done, { once: true });
    }
    return send.apply(this, arguments);
  };
})()`
const PAGE_ACTIONS = `(() => {
  if (window.__ehxAct) return;
  const describe = (el) => {
    if (!el || el.nodeType !== 1) return String(el);
    const id = el.id ? '#' + el.id : '';
    const testId = el.getAttribute('data-testid');
    const text = (el.getAttribute('aria-label') || el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 60);
    return el.tagName.toLowerCase() + id + (testId ? '[data-testid="' + testId + '"]' : '') + (text ? ' "' + text + '"' : '');
  };
  const disabledOf = (el) => el.closest('button:disabled, input:disabled, select:disabled, textarea:disabled, [aria-disabled="true"], fieldset:disabled');
  const centerOf = (el) => {
    const box = el.getBoundingClientRect();
    return { box, x: box.left + box.width / 2, y: box.top + box.height / 2 };
  };
  const SCROLLS = /(auto|scroll|overlay)/;
  function visibleFrame(el) {
    const frame = { top: 0, left: 0, bottom: innerHeight, right: innerWidth };
    for (let node = el.parentElement; node && node !== document.body && node !== document.documentElement; node = node.parentElement) {
      const style = getComputedStyle(node);
      if (SCROLLS.test(style.overflowY) || SCROLLS.test(style.overflowX)) {
        const rect = node.getBoundingClientRect();
        const top = rect.top + node.clientTop;
        const left = rect.left + node.clientLeft;
        frame.top = Math.max(frame.top, top);
        frame.left = Math.max(frame.left, left);
        frame.bottom = Math.min(frame.bottom, top + node.clientHeight);
        frame.right = Math.min(frame.right, left + node.clientWidth);
      }
      if (style.position === 'fixed') break;
    }
    return frame;
  }
  function overlaidBy(el, x, y) {
    const hit = document.elementFromPoint(x, y);
    if (!hit || hit === el || el.contains(hit) || hit.contains(el)) return null;
    for (let node = hit; node && node.nodeType === 1 && !node.contains(el); node = node.parentElement) {
      const position = getComputedStyle(node).position;
      if (position === 'fixed' || position === 'sticky') return node;
    }
    return null;
  }
  const centred = (box, frame) => {
    const x = box.left + box.width / 2;
    const y = box.top + box.height / 2;
    return x >= frame.left && x <= frame.right && y >= frame.top && y <= frame.bottom;
  };
  const REVEAL_RETRIES = 2;
  const REVEAL_WINDOW_MS = 4000;
  const reveals = new WeakMap();
  function needsReveal(el, box) {
    const frame = visibleFrame(el);
    const clipped = box.top < frame.top || box.left < frame.left || box.bottom > frame.bottom || box.right > frame.right;
    if (!clipped && !overlaidBy(el, box.left + box.width / 2, box.top + box.height / 2)) return false;
    const now = Date.now();
    const last = reveals.get(el);
    const tries = last && now - last.at < REVEAL_WINDOW_MS ? last.tries : 0;
    if (tries >= REVEAL_RETRIES && centred(box, frame)) return false;
    reveals.set(el, { tries: tries + 1, at: now });
    return true;
  }
  function reveal(el, options) {
    const target = (options && options.control && control(el)) || el;
    if (!target.isConnected || !needsReveal(target, target.getBoundingClientRect())) return false;
    target.scrollIntoView({ block: 'center', inline: 'nearest', behavior: (options && options.smooth) ? 'smooth' : 'instant' });
    return true;
  }
  function ready(el, { hitTest = true, enabled = true } = {}) {
    if (!el.isConnected) return { retry: 'the element left the page' };
    const style = getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') return { retry: 'the element is hidden' };
    let { box } = centerOf(el);
    if (!box.width || !box.height) return { retry: 'the element has no size' };
    if (enabled && disabledOf(el)) return { fail: describe(el) + ' is disabled' };
    if (needsReveal(el, box)) {
      el.scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'instant' });
      return { retry: 'scrolled into view', scrolled: true };
    }
    const { x, y } = centerOf(el);
    if (hitTest) {
      const hit = document.elementFromPoint(x, y);
      if (hit && hit !== el && !el.contains(hit) && !hit.contains(el)) {
        return { retry: describe(el) + ' is covered by ' + describe(hit) + ' at its centre', covered: true };
      }
    }
    return { x, y, box: { x: box.left, y: box.top, width: box.width, height: box.height } };
  }
  const base = (x, y, extra) => Object.assign({ bubbles: true, cancelable: true, composed: true, clientX: x, clientY: y, view: window }, extra || {});
  const pointer = (x, y, extra) => base(x, y, Object.assign({ pointerId: 1, pointerType: 'mouse', isPrimary: true }, extra || {}));
  let hovered = null;
  function leave(next) {
    const previous = hovered;
    hovered = next;
    if (!previous || previous === next || !previous.isConnected) return;
    const { x, y } = centerOf(previous);
    previous.dispatchEvent(new PointerEvent('pointerout', pointer(x, y, { relatedTarget: next })));
    previous.dispatchEvent(new MouseEvent('mouseout', base(x, y, { relatedTarget: next })));
    for (let node = previous; node && node.nodeType === 1 && !(next && node.contains(next)); node = node.parentElement) {
      node.dispatchEvent(new PointerEvent('pointerleave', pointer(x, y, { bubbles: false, relatedTarget: next })));
      node.dispatchEvent(new MouseEvent('mouseleave', base(x, y, { bubbles: false, relatedTarget: next })));
    }
  }
  function enter(el, x, y) {
    const previous = hovered;
    if (previous === el) return;
    leave(el);
    el.dispatchEvent(new PointerEvent('pointerover', pointer(x, y, { relatedTarget: previous })));
    el.dispatchEvent(new MouseEvent('mouseover', base(x, y, { relatedTarget: previous })));
    const chain = [];
    for (let node = el; node && node.nodeType === 1 && !(previous && node.contains(previous)); node = node.parentElement) chain.unshift(node);
    for (const node of chain) {
      node.dispatchEvent(new PointerEvent('pointerenter', pointer(x, y, { bubbles: false, relatedTarget: previous })));
      node.dispatchEvent(new MouseEvent('mouseenter', base(x, y, { bubbles: false, relatedTarget: previous })));
    }
    el.dispatchEvent(new PointerEvent('pointermove', pointer(x, y)));
    el.dispatchEvent(new MouseEvent('mousemove', base(x, y)));
  }
  function clickAt(el, x, y, detail) {
    el.dispatchEvent(new PointerEvent('pointerdown', pointer(x, y, { button: 0, buttons: 1, detail })));
    el.dispatchEvent(new MouseEvent('mousedown', base(x, y, { button: 0, buttons: 1, detail })));
    if (typeof el.focus === 'function' && document.activeElement !== el) el.focus({ preventScroll: true });
    el.dispatchEvent(new PointerEvent('pointerup', pointer(x, y, { button: 0, detail })));
    el.dispatchEvent(new MouseEvent('mouseup', base(x, y, { button: 0, detail })));
    el.click();
  }
  const newTabHref = (el) => {
    const link = el.closest('a[href]');
    return link && (link.target === '_blank' || link.target === '_new') ? link.href : null;
  };
  function click(el, options) {
    const state = ready(el, options);
    if (!('x' in state)) return state;
    enter(el, state.x, state.y);
    const count = (options && options.count) || 1;
    for (let n = 1; n <= count; n++) clickAt(el, state.x, state.y, n);
    if (count === 2) el.dispatchEvent(new MouseEvent('dblclick', base(state.x, state.y, { button: 0, detail: 2 })));
    return { done: true, box: state.box, newTab: newTabHref(el) };
  }
  function hover(el, options) {
    const state = ready(el, Object.assign({ enabled: false }, options || {}));
    if (!('x' in state)) return state;
    enter(el, state.x, state.y);
    return { done: true, box: state.box };
  }
  const TEXT_TYPES = new Set(['', 'text', 'search', 'email', 'url', 'tel', 'password', 'number', 'date', 'datetime-local', 'month', 'time', 'week', 'color']);
  function control(el) {
    if (el.matches('input, textarea, select, [contenteditable=""], [contenteditable="true"]')) return el;
    if (el.tagName === 'LABEL' && el.control) return el.control;
    return el.querySelector('input:not([type=hidden]), textarea, select, [contenteditable=""], [contenteditable="true"]');
  }
  function setValue(input, value) {
    const proto = input.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : input.tagName === 'SELECT' ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, 'value').set.call(input, value);
  }
  const editable = (input) => input.isContentEditable || input.tagName === 'TEXTAREA' || (input.tagName === 'INPUT' && TEXT_TYPES.has((input.getAttribute('type') || '').toLowerCase()));
  function fill(el, options) {
    const input = control(el);
    if (!input) return { fail: describe(el) + ' is not and does not contain a text input' };
    const state = ready(input, Object.assign({ hitTest: false }, options || {}));
    if (!('x' in state)) return state;
    if (input.readOnly) return { fail: describe(input) + ' is read-only' };
    if (!editable(input)) return { fail: describe(input) + ' is not a text field (use check() or select())' };
    const value = String(options.value);
    enter(input, state.x, state.y);
    input.focus({ preventScroll: true });
    if (input.isContentEditable) {
      input.textContent = value;
    } else {
      setValue(input, value);
    }
    input.dispatchEvent(new InputEvent('input', { bubbles: true, composed: true, inputType: 'insertReplacementText', data: value }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    return { done: true, box: state.box };
  }
  const KEY_ALIASES = { Space: ' ', Esc: 'Escape', Return: 'Enter', Del: 'Delete', Up: 'ArrowUp', Down: 'ArrowDown', Left: 'ArrowLeft', Right: 'ArrowRight' };
  const KEY_CODES = { Enter: 13, Tab: 9, Escape: 27, Backspace: 8, Delete: 46, ' ': 32, ArrowUp: 38, ArrowDown: 40, ArrowLeft: 37, ArrowRight: 39, Home: 36, End: 35, PageUp: 33, PageDown: 34 };
  function parseKey(spec) {
    const parts = String(spec).split('+');
    const raw = parts.pop() || '+';
    const mods = new Set(parts.map((part) => part.toLowerCase()));
    const mac = /Mac/.test(navigator.platform);
    const key = KEY_ALIASES[raw] || raw;
    return {
      key,
      ctrlKey: mods.has('control') || mods.has('ctrl') || (mods.has('controlormeta') && !mac),
      metaKey: mods.has('meta') || mods.has('cmd') || (mods.has('controlormeta') && mac),
      shiftKey: mods.has('shift'),
      altKey: mods.has('alt'),
    };
  }
  function keyEvent(type, parsed) {
    const { key } = parsed;
    const code = key.length === 1 ? (/[a-z]/i.test(key) ? 'Key' + key.toUpperCase() : /[0-9]/.test(key) ? 'Digit' + key : key === ' ' ? 'Space' : '') : key;
    const event = new KeyboardEvent(type, { key, code, bubbles: true, cancelable: true, composed: true, ctrlKey: parsed.ctrlKey, metaKey: parsed.metaKey, shiftKey: parsed.shiftKey, altKey: parsed.altKey });
    const legacy = KEY_CODES[key] ?? (key.length === 1 ? key.toUpperCase().charCodeAt(0) : 0);
    Object.defineProperty(event, 'keyCode', { get: () => legacy });
    Object.defineProperty(event, 'which', { get: () => legacy });
    return event;
  }
  function insertText(input, text, inputType) {
    const before = new InputEvent('beforeinput', { bubbles: true, cancelable: true, composed: true, inputType, data: text || null });
    if (!input.dispatchEvent(before)) return false;
    if (input.isContentEditable) {
      const selection = getSelection();
      if (inputType === 'insertText') {
        if (!selection.rangeCount || !input.contains(selection.anchorNode)) {
          const range = document.createRange();
          range.selectNodeContents(input);
          range.collapse(false);
          selection.removeAllRanges();
          selection.addRange(range);
        }
        const range = selection.getRangeAt(0);
        range.deleteContents();
        range.insertNode(document.createTextNode(text));
        range.collapse(false);
      } else {
        const range = selection.rangeCount ? selection.getRangeAt(0) : null;
        if (range && !range.collapsed) range.deleteContents();
        else if (input.textContent) input.textContent = input.textContent.slice(0, -1);
      }
    } else {
      const value = input.value;
      const supportsCaret = typeof input.selectionStart === 'number';
      let start = supportsCaret ? input.selectionStart : value.length;
      let end = supportsCaret ? input.selectionEnd : value.length;
      if (inputType === 'deleteContentBackward' && start === end) start = Math.max(0, start - 1);
      if (inputType === 'deleteContentForward' && start === end) end = Math.min(value.length, end + 1);
      setValue(input, value.slice(0, start) + (text || '') + value.slice(end));
      if (supportsCaret) {
        const caret = start + (text || '').length;
        try { input.setSelectionRange(caret, caret); } catch (e) {}
      }
    }
    input.dispatchEvent(new InputEvent('input', { bubbles: true, composed: true, inputType, data: text || null }));
    return true;
  }
  const TABBABLE = 'a[href], button:not(:disabled), input:not(:disabled):not([type=hidden]), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"]), [contenteditable=""], [contenteditable="true"]';
  function moveFocus(from, backwards) {
    const list = [...document.querySelectorAll(TABBABLE)].filter((node) => {
      const box = node.getBoundingClientRect();
      return box.width && box.height && getComputedStyle(node).visibility !== 'hidden';
    });
    if (!list.length) return;
    const index = list.indexOf(from);
    const next = list[(index + (backwards ? -1 : 1) + list.length) % list.length];
    next.focus();
  }
  function keyTarget(el) {
    if (el) return el;
    const active = document.activeElement;
    return active && active !== document.body ? active : document.body;
  }
  function press(el, options) {
    const target = keyTarget(el);
    if (el && target !== document.activeElement && typeof target.focus === 'function') target.focus({ preventScroll: true });
    const parsed = parseKey(options.key);
    const { key } = parsed;
    const chord = parsed.ctrlKey || parsed.metaKey || parsed.altKey;
    let proceed = target.dispatchEvent(keyEvent('keydown', parsed));
    const text = !chord && key.length === 1 ? key : null;
    const field = control(target) === target && editable(target) && !target.readOnly && !disabledOf(target) ? target : null;
    let action = null;
    if (proceed && text) {
      proceed = target.dispatchEvent(keyEvent('keypress', parsed));
      if (proceed && field) { insertText(field, text, 'insertText'); action = 'inserted'; }
      else if (proceed && text === ' ' && target.matches('button, [role=button], [role=checkbox], [role=switch], [role=radio], input[type=checkbox], input[type=radio]')) { target.click(); action = 'activated'; }
    } else if (proceed) {
      if (key === 'Enter') {
        if (target.tagName === 'TEXTAREA' || (field && field.isContentEditable)) { insertText(field, '\\n', 'insertLineBreak'); action = 'newline'; }
        else if (target.matches('button, a[href], [role=button], [role=link], [role=menuitem], [role=option], [role=tab], input[type=submit], input[type=button]')) { target.click(); action = 'activated'; }
        else if (target.form && target.tagName === 'INPUT') { target.form.requestSubmit(); action = 'submitted'; }
      } else if (key === 'Tab') { moveFocus(target, parsed.shiftKey); action = 'focus moved'; }
      else if (field && key === 'Backspace') { insertText(field, '', 'deleteContentBackward'); action = 'deleted'; }
      else if (field && key === 'Delete') { insertText(field, '', 'deleteContentForward'); action = 'deleted'; }
      else if (field && chord && key.toLowerCase() === 'a' && !field.isContentEditable) { field.select(); action = 'selected all'; }
    }
    target.dispatchEvent(keyEvent('keyup', parsed));
    return { done: true, target: describe(target), action };
  }
  function type(el, options) {
    const input = control(el) || el;
    if (!editable(input)) return { fail: describe(input) + ' is not a text field' };
    const state = ready(input, Object.assign({ hitTest: false }, options || {}));
    if (!('x' in state)) return state;
    if (document.activeElement !== input) input.focus({ preventScroll: true });
    const character = String(options.character);
    const parsed = { key: character === '\\n' ? 'Enter' : character, ctrlKey: false, metaKey: false, altKey: false, shiftKey: character !== character.toLowerCase() };
    if (input.dispatchEvent(keyEvent('keydown', parsed)) && input.dispatchEvent(keyEvent('keypress', parsed))) {
      insertText(input, character === '\\n' ? '\\n' : character, character === '\\n' ? 'insertLineBreak' : 'insertText');
    }
    input.dispatchEvent(keyEvent('keyup', parsed));
    return { done: true, box: state.box };
  }
  function scroller(el) {
    for (let node = el; node && node.nodeType === 1; node = node.parentElement) {
      const style = getComputedStyle(node);
      const y = /(auto|scroll|overlay)/.test(style.overflowY) && node.scrollHeight > node.clientHeight;
      const x = /(auto|scroll|overlay)/.test(style.overflowX) && node.scrollWidth > node.clientWidth;
      if (x || y) return node;
    }
    return document.scrollingElement || document.documentElement;
  }
  function wheel(el, options) {
    const state = ready(el, Object.assign({ enabled: false, hitTest: false }, options || {}));
    if (!('x' in state)) return state;
    enter(el, state.x, state.y);
    const dx = options.deltaX || 0;
    const dy = options.deltaY === undefined ? 400 : options.deltaY;
    const allowed = el.dispatchEvent(new WheelEvent('wheel', base(state.x, state.y, { deltaX: dx, deltaY: dy, deltaMode: 0 })));
    if (!allowed) return { done: true, prevented: true, box: state.box };
    const target = scroller(el);
    const before = { left: target.scrollLeft, top: target.scrollTop };
    target.scrollBy({ left: dx, top: dy, behavior: 'instant' });
    return { done: true, scrolled: describe(target), from: before, to: { left: target.scrollLeft, top: target.scrollTop }, box: state.box };
  }
  function checkState(el) {
    const input = el.matches('input[type=checkbox], input[type=radio]') ? el : el.querySelector('input[type=checkbox], input[type=radio]');
    if (input) return { node: input.closest('label') && !el.contains(input) ? el : input, checked: input.checked };
    const role = el.closest('[role=checkbox], [role=switch], [role=radio], [role=menuitemcheckbox]');
    if (role) return { node: role, checked: role.getAttribute('aria-checked') === 'true' || role.getAttribute('data-state') === 'checked' };
    return null;
  }
  function check(el, options) {
    const current = checkState(el);
    if (!current) return { fail: describe(el) + ' is not a checkbox, radio or switch' };
    if (current.checked === options.on) return { done: true, changed: false };
    const outcome = click(current.node, options);
    if (!outcome.done) return outcome;
    return { done: true, changed: true, box: outcome.box };
  }
  window.__ehxAct = { click, hover, fill, press, type, wheel, check, describe, reveal };
})()`

function loadPlaywright() {
  const candidates = [...(CONFIG.playwrightFrom ?? []), path.dirname(new URL(import.meta.url).pathname), process.cwd()].filter(Boolean)
  const failures = []
  for (const dir of candidates) {
    try {
      return createRequire(path.join(dir, 'package.json'))('playwright')
    } catch (error) {
      failures.push(`${dir}: ${error.message}`)
    }
  }
  throw new Error(`Could not load playwright.\n${failures.join('\n')}\nInstall it with: eks-harness driver install web (or set web.playwrightFrom to a folder with node_modules/playwright).`)
}

const { chromium } = loadPlaywright()

fs.mkdirSync(ARTIFACTS_DIR, { recursive: true })
fs.mkdirSync(STATE_DIR, { recursive: true })

const session = {
  sid: LEASE_SID,
  browser: null,
  cdpUrl: null,
  context: null,
  page: null,
  mobile: null,
  recording: null,
  console: [],
  pageErrors: [],
  failedRequests: [],
  wsFrames: [],
  extensionState: {},
}

const MAX_BUFFER = 500
const push = (buffer, entry) => {
  buffer.push({ at: new Date().toISOString(), ...entry })
  if (buffer.length > MAX_BUFFER) buffer.shift()
}

const activePage = () => session.recording?.page ?? session.page

const log = (message) => process.stdout.write(`[web-driver ${new Date().toISOString()}] ${message}\n`)

process.on('unhandledRejection', (error) => log(`unhandled rejection: ${error?.stack ?? error}`))
process.on('uncaughtException', (error) => log(`uncaught exception: ${error?.stack ?? error}`))
for (const signal of ['SIGTERM', 'SIGINT']) {
  process.on(signal, () => {
    log(`${signal}: closing the browser and exiting`)
    Promise.resolve(session.browser?.close())
      .catch(() => {})
      .finally(() => process.exit(0))
    setTimeout(() => process.exit(0), 3_000).unref()
  })
}

const SESSION_FILE = path.join(STATE_DIR, 'session.json')

function persistSession() {
  try {
    const page = session.page
    const data = {
      lastUrl: page && !page.isClosed() ? page.url() : null,
      baseUrl: WEB_BASE_URL,
      extensions: session.extensionState,
    }
    fs.writeFileSync(SESSION_FILE, JSON.stringify(data), { mode: 0o600 })
  } catch (error) {
    log(`could not persist session: ${error.message}`)
  }
}

function loadPersistedSession() {
  try {
    const data = JSON.parse(fs.readFileSync(SESSION_FILE, 'utf8'))
    if (data.baseUrl !== WEB_BASE_URL) return null
    return data
  } catch {
    return null
  }
}

const WS_PAYLOAD_LIMIT = Number(opt('wsPayloadLimit', 600))
const WS_BUFFER = Number(opt('wsBuffer', 2000))

function wsPayload(payload) {
  const text = typeof payload === 'string'
    ? payload.replace(/\x1e/g, '\\x1e')
    : `base64:${Buffer.from(payload).toString('base64')}`
  return text.length > WS_PAYLOAD_LIMIT
    ? `${text.slice(0, WS_PAYLOAD_LIMIT)}... (${text.length} chars)`
    : text
}

function pushWs(entry) {
  session.wsFrames.push({ at: new Date().toISOString(), ...entry })
  if (session.wsFrames.length > WS_BUFFER) session.wsFrames.shift()
}

const WS_IGNORE = CONFIG.wsIgnore ? new RegExp(CONFIG.wsIgnore) : null

function instrumentWebSockets(page, label) {
  page.on('websocket', (ws) => {
    const url = ws.url()
    if (WS_IGNORE && WS_IGNORE.test(url)) return
    pushWs({ page: label, url, direction: 'open' })
    ws.on('framesent', (frame) => pushWs({ page: label, url, direction: 'sent', payload: wsPayload(frame.payload) }))
    ws.on('framereceived', (frame) => pushWs({ page: label, url, direction: 'received', payload: wsPayload(frame.payload) }))
    ws.on('socketerror', (error) => pushWs({ page: label, url, direction: 'error', payload: String(error) }))
    ws.on('close', () => pushWs({ page: label, url, direction: 'close' }))
  })
}

function instrument(page, label = 'desktop') {
  instrumentWebSockets(page, label)
  page.on('console', (message) => {
    push(session.console, { type: message.type(), text: message.text(), url: page.url() })
  })
  page.on('pageerror', (error) => {
    push(session.pageErrors, { message: error.message, stack: error.stack })
  })
  page.on('requestfailed', (request) => {
    push(session.failedRequests, {
      url: request.url(),
      method: request.method(),
      failure: request.failure()?.errorText,
    })
  })
  page.on('response', (response) => {
    if (response.status() >= 400) {
      push(session.failedRequests, {
        url: response.url(),
        method: response.request().method(),
        status: response.status(),
      })
    }
  })
}

const INSECURE_TLS = Boolean(opt('insecureTls', false))

const BYPASS_CSP = Boolean(opt('bypassCsp', false))

function contextOptions(profile, extra = {}) {
  const { label, ...rest } = profile
  return {
    ...rest,
    locale: LOCALE,
    ...(TIMEZONE ? { timezoneId: TIMEZONE } : {}),
    bypassCSP: BYPASS_CSP,
    ...(INSECURE_TLS ? { ignoreHTTPSErrors: true } : {}),
    ...extra,
  }
}

async function connectBrowser() {
  const endpoint = await chromeEndpoint(session.sid)
  if (session.browser?.isConnected() && session.cdpUrl === endpoint) return
  if (session.browser) await session.browser.close().catch(() => {})
  session.browser = await chromium.connectOverCDP(endpoint, { timeout: 30_000 })
  session.cdpUrl = endpoint
  session.context = null
  session.page = null
  session.mobile = null
  session.recording = null
  session.browser.on('disconnected', () => log('the shared Chrome connection closed'))
  log(`connected to the leased profile in the shared Chrome (${endpoint})`)
}

async function newProfileContext(profile) {
  const context = await session.browser.newContext(contextOptions(profile))
  context.setDefaultTimeout(15_000)
  await context.addInitScript(POINTER_OVERLAY)
  await context.addInitScript(ANNOTATION_OVERLAY)
  await context.addInitScript(NETWORK_TRACKER)
  await context.addInitScript(PAGE_ACTIONS)
  for (const ext of EXTENSIONS) if (ext.onContext) await ext.onContext(context)
  context.on('close', () => {
    if (session.context === context) {
      session.context = null
      session.page = null
    }
    if (session.mobile?.context === context) session.mobile = null
  })
  return context
}

async function reportContexts() {
  const sid = session.sid
  if (!sid) return
  const ids = []
  for (const context of [session.context, session.mobile?.context]) {
    const page = context?.pages()[0]
    if (!page) continue
    const cdp = await context.newCDPSession(page)
    try {
      const { targetInfo } = await cdp.send('Target.getTargetInfo')
      if (targetInfo?.browserContextId) ids.push(targetInfo.browserContextId)
    } finally {
      await cdp.detach().catch(() => {})
    }
  }
  await updateLeaseMeta(sid, { browserContextIds: ids })
    .catch((error) => log(`could not report the browser contexts to lease ${sid}: ${error.message}`))
}

async function ensureBrowser() {
  if (session.browser?.isConnected() && session.page && !session.page.isClosed()) return
  await connectBrowser()
  const created = !session.context
  if (created) session.context = await newProfileContext(VIEWPORTS.desktop)
  session.page = await session.context.newPage()
  instrument(session.page)
  if (created) await reportContexts()
  await session.page.goto(WEB_BASE_URL, { waitUntil: 'domcontentloaded' }).catch(() => {})
  const restored = loadPersistedSession()
  if (restored) {
    session.extensionState = restored.extensions ?? {}
    for (const ext of EXTENSIONS) {
      if (ext.onRestore) await ext.onRestore(session.extensionState[ext.id] ?? null, session.page).catch((error) => log(`${ext.id} restore failed: ${error.message}`))
    }
    if (restored.lastUrl?.startsWith(WEB_BASE_URL)) {
      await session.page.goto(restored.lastUrl, { waitUntil: 'domcontentloaded' }).catch(() => {})
      await settle(session.page, { timeout: 10_000 })
    }
    log(`session restored at ${restored.lastUrl}`)
  }
}

async function ensureMobile() {
  if (session.mobile) return session.mobile
  await ensureBrowser()
  const context = await newProfileContext(VIEWPORTS.mobile)
  const page = await context.newPage()
  instrument(page, 'mobile')
  session.mobile = { context, page }
  await reportContexts()
  await mirrorDesktopStorage(page)
  return session.mobile
}

async function readPageStorage(page) {
  if (!page || page.isClosed() || !page.url().startsWith(WEB_BASE_URL)) return null
  const storage = await page.evaluate(() => ({
    local: Object.fromEntries(Object.keys(localStorage).map((key) => [key, localStorage.getItem(key)])),
    session: Object.fromEntries(Object.keys(sessionStorage).map((key) => [key, sessionStorage.getItem(key)])),
  })).catch(() => null)
  if (!storage) return null
  storage.cookies = await page.context().cookies(WEB_BASE_URL).catch(() => [])
  return storage
}

async function writePageStorage(page, storage) {
  if (!page.url().startsWith(WEB_BASE_URL)) {
    await page.goto(WEB_BASE_URL, { waitUntil: 'domcontentloaded' })
  }
  if (storage.cookies?.length) await page.context().addCookies(storage.cookies).catch(() => {})
  await page.evaluate(({ local, session: stored }) => {
    localStorage.clear()
    sessionStorage.clear()
    for (const [key, value] of Object.entries(local ?? {})) localStorage.setItem(key, value)
    for (const [key, value] of Object.entries(stored ?? {})) sessionStorage.setItem(key, value)
  }, storage)
}

async function mirrorDesktopStorage(page) {
  if (page === session.page) return { mirrored: true, changed: false }
  const storage = await readPageStorage(session.page)
  if (!storage) return { mirrored: false, changed: false }
  const before = await readPageStorage(page)
  const changed = JSON.stringify(before?.local) !== JSON.stringify(storage.local) || JSON.stringify(before?.session) !== JSON.stringify(storage.session)
  if (changed) await writePageStorage(page, storage)
  return { mirrored: true, changed }
}

async function syncContexts() {
  if (session.mobile) await mirrorDesktopStorage(session.mobile.page).catch(() => {})
  persistSession()
}

async function signOutPage(page) {
  await page.context().clearCookies().catch(() => {})
  if (!page.url().startsWith(WEB_BASE_URL)) {
    await page.goto(WEB_BASE_URL, { waitUntil: 'domcontentloaded' })
  }
  await page.evaluate(() => {
    localStorage.clear()
    sessionStorage.clear()
  })
}

function frameTime(metadata) {
  const now = Date.now() / 1000
  const swapped = Number(metadata?.timestamp)
  return Number.isFinite(swapped) && Math.abs(now - swapped) < 5 ? swapped : now
}

const CDP_CAPTURE_TIMEOUT_MS = Number(opt('cdpCaptureTimeoutMs', 10_000))
function withTimeout(promise, ms, what) {
  let timer
  const expired = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${what} did not answer within ${ms}ms: the page is not painting (renderer busy or blocked by a dialog)`)), ms)
  })
  return Promise.race([promise, expired]).finally(() => clearTimeout(timer))
}

const ENCODER = CONFIG.encoder ?? ['python3', '-m', 'eks_harness.capture.encode']

function runEncoder(args) {
  const [command, ...prefix] = ENCODER
  const result = spawnSync(command, [...prefix, ...args], { encoding: 'utf8', maxBuffer: 16 * 1024 * 1024, env: { ...process.env, ...(LEASE_SID ? { EHX_LEASE_SID: LEASE_SID } : {}), ...(CONFIG.encoderEnv ?? {}) } })
  return result
}


const PACE_DEFAULTS = { moveMs: 300, dwellMs: 400, typeMsPerChar: 40, holdMs: 700, screenHoldMs: 1500, mobilePressMs: 250 }
const PACE_FAST = { moveMs: 0, dwellMs: 100, typeMsPerChar: 0, holdMs: 150, screenHoldMs: 400, mobilePressMs: 0 }
const paceEnv = (name, fallback) => {
  const raw = process.env[name]
  if (raw === undefined || raw === '') return fallback
  const value = Number(raw)
  if (!Number.isInteger(value) || value < 0) throw new Error(`${name} must be a non-negative integer of ms (got ${raw})`)
  return value
}
const PACE_ENV = {
  moveMs: paceEnv('EHX_PACE_MOVE_MS', PACE_DEFAULTS.moveMs),
  dwellMs: paceEnv('EHX_PACE_DWELL_MS', PACE_DEFAULTS.dwellMs),
  typeMsPerChar: paceEnv('EHX_PACE_TYPE_MS_PER_CHAR', PACE_DEFAULTS.typeMsPerChar),
  holdMs: paceEnv('EHX_PACE_HOLD_MS', PACE_DEFAULTS.holdMs),
  screenHoldMs: paceEnv('EHX_PACE_SCREEN_HOLD_MS', PACE_DEFAULTS.screenHoldMs),
  mobilePressMs: paceEnv('EHX_PACE_MOBILE_PRESS_MS', PACE_DEFAULTS.mobilePressMs),
  ...(CONFIG.pace ?? {}),
}
const PACE_KEYS = [['moveMs', 'move_ms'], ['dwellMs', 'dwell_ms'], ['typeMsPerChar', 'type_ms_per_char'],
  ['holdMs', 'hold_ms'], ['screenHoldMs', 'screen_hold_ms'], ['mobilePressMs', 'mobile_press_ms']]
function parsePace(spec) {
  if (spec === undefined || spec === null || spec === '' || spec === 'demo') return { name: 'demo', ...PACE_ENV }
  if (spec === 'fast') return { name: 'fast', ...PACE_FAST }
  let object
  try {
    object = typeof spec === 'string' ? JSON.parse(spec) : spec
  } catch {
    throw new Error(`--pace takes demo, fast or a JSON object (got ${JSON.stringify(spec)?.slice(0, 80)})`)
  }
  if (!object || typeof object !== 'object' || Array.isArray(object)) {
    throw new Error('--pace takes demo, fast or a JSON object')
  }
  const known = new Set(PACE_KEYS.flat())
  for (const key of Object.keys(object)) {
    if (!known.has(key)) throw new Error(`unknown pace key ${JSON.stringify(key)}`)
  }
  const merged = { ...PACE_ENV }
  for (const [camel, snake] of PACE_KEYS) {
    const value = object[camel] ?? object[snake]
    if (value === undefined) continue
    if (!Number.isInteger(value) || value < 0) throw new Error(`pace.${camel} must be a non-negative integer of ms`)
    merged[camel] = value
  }
  return { name: typeof spec === 'string' ? spec : 'custom', ...merged }
}
const paceSleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

const MOTION_W = 192
const MOTION_H = 108
const MOTION_PIXEL_DELTA = Number(opt('motionPixelDelta', 10))
const MOTION_MIN_CHANGED_PIXELS = Number(opt('motionMinPixels', 20))

function looksStatic(file, duration, skip = 2) {
  if (duration <= skip + 0.5) return { static: false, checked: false, reason: 'clip too short to judge' }
  const result = spawnSync(
    'ffmpeg',
    ['-v', 'error', '-ss', String(skip), '-i', file,
     '-vf', `fps=4,scale=${MOTION_W}:${MOTION_H},format=gray`, '-f', 'rawvideo', '-'],
    { maxBuffer: 256 * 1024 * 1024 },
  )
  if (result.status !== 0 || !result.stdout?.length) return { static: false, checked: false, reason: 'decode failed' }
  const size = MOTION_W * MOTION_H
  const frames = Math.floor(result.stdout.length / size)
  let changedPairs = 0
  let maxChanged = 0
  for (let f = 1; f < frames; f++) {
    let changed = 0
    const prev = (f - 1) * size
    const cur = f * size
    for (let i = 0; i < size; i++) {
      if (Math.abs(result.stdout[cur + i] - result.stdout[prev + i]) > MOTION_PIXEL_DELTA) changed++
    }
    maxChanged = Math.max(maxChanged, changed)
    if (changed >= MOTION_MIN_CHANGED_PIXELS) changedPairs++
  }
  return {
    static: changedPairs === 0,
    checked: true,
    sampledFrames: frames,
    framesWithChange: changedPairs,
    maxChangedPixels: maxChanged,
    note: 'hint only: diff of 192x108 frames at 4 fps after the first 2s',
  }
}

const SETTLE_LIVE_MS = Number(opt('settleLiveMs', 900))
async function settle(page, { timeout = 8_000, quiet = 250, domQuiet = 150, liveMs = SETTLE_LIVE_MS } = {}) {
  const started = Date.now()
  let timedOut = false
  let live = false
  await page
    .waitForFunction(
      ([quietMs, domQuietMs, since, liveAfter, react]) => {
        const net = window.__ehxNet
        if (!net) return document.readyState !== 'loading'
        const root = document.querySelector('main') || document.body
        const hydrated = !react || !root || Object.keys(root).some((key) => key.startsWith('__reactFiber$'))
        const now = Date.now()
        if (!hydrated || net.inflight > 0 || now - net.last < quietMs) return false
        if (now - net.mutated >= domQuietMs) return 'quiet'
        return liveAfter > 0 && now - since >= liveAfter ? 'live' : false
      },
      [quiet, domQuiet, started, liveMs, Boolean(opt('settleReact', false))],
      { timeout, polling: 40 },
    )
    .then((handle) => handle.jsonValue().then((value) => { live = value === 'live' }))
    .catch(() => {
      timedOut = true
    })
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))).catch(() => {})
  return { ms: Date.now() - started, timedOut, live }
}

async function gotoPath(target = '/') {
  const page = activePage()
  const url = target.startsWith('http') ? target : `${WEB_BASE_URL}${target.startsWith('/') ? '' : '/'}${target}`
  await page.goto(url, { waitUntil: 'domcontentloaded' })
  await settle(page, { timeout: 15_000 })
  persistSession()
  return page.url()
}

async function shotHere(page, file) {
  const profile = VIEWPORTS.mobile
  const desktop = page.viewportSize() ?? VIEWPORTS.desktop.viewport
  const cdp = await page.context().newCDPSession(page)
  try {
    await cdp.send('Emulation.setDeviceMetricsOverride', {
      width: profile.viewport.width,
      height: profile.viewport.height,
      deviceScaleFactor: profile.deviceScaleFactor,
      mobile: true,
    })
    await cdp.send('Emulation.setTouchEmulationEnabled', { enabled: true, maxTouchPoints: 5 })
    await cdp.send('Network.setUserAgentOverride', { userAgent: profile.userAgent })
    await page.evaluate(() => window.dispatchEvent(new Event('resize')))
    await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))))
    await page.waitForTimeout(Number(opt('hereSettleMs', 400)))
    const { data } = await cdp.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false })
    fs.writeFileSync(file, Buffer.from(data, 'base64'))
  } finally {
    await cdp.send('Emulation.clearDeviceMetricsOverride').catch(() => {})
    await cdp.send('Emulation.setTouchEmulationEnabled', { enabled: false }).catch(() => {})
    await cdp.send('Network.setUserAgentOverride', { userAgent: '' }).catch(() => {})
    await cdp.detach().catch(() => {})
    await page.setViewportSize(desktop).catch(() => {})
  }
}

async function ensureActions(page) {
  const installed = await page.evaluate(() => typeof window.__ehxAct === 'object').catch(() => false)
  if (!installed) await page.evaluate(PAGE_ACTIONS)
}

const nextFrames = (page) => page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))).catch(() => {})

async function inPage(page, locator, action, options = {}, { timeout = 10_000 } = {}) {
  await ensureActions(page)
  const started = Date.now()
  for (;;) {
    const left = Math.max(1_000, timeout - (Date.now() - started))
    const outcome = await locator.evaluate((el, [name, args]) => window.__ehxAct[name](el, args), [action, options], { timeout: left })
    if (outcome?.done) return outcome
    if (outcome?.fail) throw new Error(`${action}: ${outcome.fail}`)
    if (Date.now() - started >= timeout) {
      const hint = outcome?.covered ? '; { force: true } skips the hit test, { real: true } uses trusted input' : ''
      throw new Error(`${action}: ${outcome?.retry ?? 'the element never became actionable'} (gave up after ${timeout}ms${hint})`)
    }
    await nextFrames(page)
    if (!outcome?.scrolled) await page.waitForTimeout(80)
  }
}

async function pressInPage(page, key, locator = null) {
  await ensureActions(page)
  if (locator) return inPage(page, locator, 'press', { key })
  return page.evaluate((spec) => window.__ehxAct.press(null, { key: spec }), key)
}

const hoverStates = new WeakMap()

async function releaseHover(page) {
  const state = hoverStates.get(page)
  if (!state?.nodeIds.length) return
  const ids = state.nodeIds
  state.nodeIds = []
  for (const nodeId of ids) await state.cdp.send('CSS.forcePseudoState', { nodeId, forcedPseudoClasses: [] }).catch(() => {})
}

async function forceHover(page, locator) {
  let state = hoverStates.get(page)
  if (!state) {
    const cdp = await page.context().newCDPSession(page)
    await cdp.send('DOM.enable')
    await cdp.send('CSS.enable')
    state = { cdp, nodeIds: [] }
    hoverStates.set(page, state)
    page.on('framenavigated', (frame) => {
      if (frame === page.mainFrame()) state.nodeIds = []
    })
  }
  await releaseHover(page)
  const mark = `h${Date.now().toString(36)}`
  await locator.evaluate((el, value) => {
    for (let node = el; node && node.nodeType === 1; node = node.parentElement) node.setAttribute('data-ehx-hover', value)
  }, mark)
  try {
    const { root } = await state.cdp.send('DOM.getDocument', { depth: 0 })
    const { nodeIds } = await state.cdp.send('DOM.querySelectorAll', { nodeId: root.nodeId, selector: `[data-ehx-hover="${mark}"]` })
    for (const nodeId of nodeIds) await state.cdp.send('CSS.forcePseudoState', { nodeId, forcedPseudoClasses: ['hover'] })
    state.nodeIds = nodeIds
  } finally {
    await page.evaluate((value) => {
      for (const node of document.querySelectorAll(`[data-ehx-hover="${value}"]`)) node.removeAttribute('data-ehx-hover')
    }, mark).catch(() => {})
  }
  return state.nodeIds.length
}

async function hoverInPage(page, locator, timeout) {
  const outcome = await inPage(page, locator, 'hover', {}, { timeout })
  return { ...outcome, forcedHover: await forceHover(page, locator) }
}

async function wheelInPage(page, locator, deltaX = 0, deltaY = 400, timeout = 10_000) {
  const tag = await locator.evaluate((el) => el.tagName, undefined, { timeout })
  if (tag === 'IFRAME') {
    const frame = await locator.contentFrame()
    const moved = await frame.evaluate(([dx, dy]) => {
      const scroller = document.scrollingElement || document.documentElement
      const from = { left: scroller.scrollLeft, top: scroller.scrollTop }
      scroller.scrollBy({ left: dx, top: dy, behavior: 'instant' })
      return { from, to: { left: scroller.scrollLeft, top: scroller.scrollTop } }
    }, [deltaX, deltaY])
    return { done: true, scrolled: 'the iframe document', ...moved }
  }
  return inPage(page, locator, 'wheel', { deltaX, deltaY }, { timeout })
}

function resolveLocator(page, { selector, text, testId, role, name }) {
  if (testId) return page.getByTestId(testId)
  if (role) return page.getByRole(role, name ? { name } : undefined)
  if (text) return page.getByText(text, { exact: false }).first()
  if (selector) return page.locator(selector).first()
  throw new Error('one of selector / text / testId / role is required')
}

async function setPointerVisible(page, visible) {
  await page.evaluate((show) => {
    const dot = document.getElementById('ehx-pointer-dot')
    if (dot) dot.style.display = show ? '' : 'none'
  }, visible).catch(() => {})
}

const HIDE_STYLE_ID = 'ehx-hide-style'
function hideSelectorsFor(extra = []) {
  const base = ['#ehx-pointer-dot', ...(CONFIG.hideSelectors ?? [])]
  const extraList = Array.isArray(extra) ? extra : String(extra ?? '').split('\n').filter(Boolean)
  return [...new Set([...base, ...extraList].map((selector) => String(selector).trim()).filter(Boolean))]
}
async function applyHide(page, selectors) {
  if (!selectors.length) return
  await page.evaluate(([id, css]) => {
    let style = document.getElementById(id)
    if (!style) {
      style = document.createElement('style')
      style.id = id
      document.documentElement.appendChild(style)
    }
    style.textContent = `${css} { display: none !important; }`
  }, [HIDE_STYLE_ID, selectors.join(',\n')]).catch(() => {})
}
async function clearHide(page) {
  await page.evaluate((id) => document.getElementById(id)?.remove(), HIDE_STYLE_ID).catch(() => {})
}

const PACED_ACTIONS = new Set(['click', 'fill', 'press', 'hover', 'wheel'])
const MARKED_ACTIONS = new Set(['click', 'fill'])
async function showPressMark(page, box) {
  return page.evaluate(({ x, y, width, height }) => new Promise((resolve) => {
    if (!window.__ehxMark?.show(x, y, width, height)) return resolve(false)
    requestAnimationFrame(() => requestAnimationFrame(() => resolve(true)))
  }), box).catch(() => false)
}
async function hidePressMark(page) {
  await page.evaluate(() => window.__ehxMark?.hide()).catch(() => {})
}
const REVEAL_SETTLE_MS = 1_200
async function revealForLead(page, locator) {
  await ensureActions(page)
  const moved = await locator.evaluate((el) => window.__ehxAct.reveal(el, { control: true, smooth: true }), undefined, { timeout: 5_000 }).catch(() => false)
  if (!moved) return false
  const started = Date.now()
  let last = null
  while (Date.now() - started < REVEAL_SETTLE_MS) {
    await nextFrames(page)
    const box = await locator.boundingBox().catch(() => null)
    if (box && last && Math.abs(box.x - last.x) < 0.5 && Math.abs(box.y - last.y) < 0.5) break
    last = box
  }
  return true
}
async function leadTo(page, locator, pace, { mark = false } = {}) {
  let box = null
  try {
    await revealForLead(page, locator)
    box = await locator.boundingBox()
    if (box) {
      await page.evaluate(([x, y, ms]) => window.__ehxPointerTo?.(x, y, ms),
        [box.x + box.width / 2, box.y + box.height / 2, pace.moveMs]).catch(() => {})
      const moved = await locator.boundingBox().catch(() => null)
      if (moved && (Math.abs(moved.x - box.x) > 4 || Math.abs(moved.y - box.y) > 4)) {
        box = moved
        await page.evaluate(([x, y, ms]) => window.__ehxPointerTo?.(x, y, ms),
          [box.x + box.width / 2, box.y + box.height / 2, 140]).catch(() => {})
      }
    }
  } catch {}
  if (mark && box) await showPressMark(page, box)
  if (pace.dwellMs) await paceSleep(pace.dwellMs)
  return box
}
async function presentationLead(page, body, pace, action) {
  let locator = null
  try { locator = resolveLocator(page, body) } catch {}
  if (!locator) {
    if (pace.dwellMs) await paceSleep(pace.dwellMs)
    return null
  }
  return leadTo(page, locator, pace, { mark: MARKED_ACTIONS.has(action) })
}
async function presentationHold(page, pace, urlBefore) {
  let changed = false
  try { changed = page.url() !== urlBefore } catch {}
  if (!changed) {
    changed = await page.evaluate(
      () => document.querySelectorAll('[role=dialog],[aria-modal="true"]').length > 0).catch(() => false)
  }
  await paceSleep(changed ? pace.screenHoldMs : pace.holdMs)
}
const recPace = () => session.recording?.pace ?? null
async function scriptLead(page, target) {
  const pace = recPace()
  if (!pace) return null
  let locator = null
  try { locator = locate(page, target) } catch {}
  if (!locator) {
    if (pace.dwellMs) await paceSleep(pace.dwellMs)
    return null
  }
  return leadTo(page, locator, pace, { mark: true })
}
function stampAction(box = null) {
  const rec = session.recording
  if (!rec) return
  rec.actionAt = Date.now()
  rec.actionBox = box
}
async function pressed(page, action) {
  try {
    return await action()
  } finally {
    if (session.recording) await hidePressMark(page)
  }
}
async function scriptHold(page, urlBefore = null) {
  const pace = recPace()
  if (!pace) return
  await presentationHold(page, pace, urlBefore)
}
function scriptTarget(args) {
  if (!Array.isArray(args)) return {}
  const first = args[0]
  if (first && typeof first === 'object' && !Array.isArray(first)) return describeTarget(first)
  if (typeof first === 'string') return { text: first.slice(0, 200) }
  return {}
}

const CROP_COVERAGE_LIMIT = Number(opt('cropMaxCoverage', 0.85))

async function focusClip(page, selector, pad) {
  const locator = page.locator(selector).first()
  await locator.scrollIntoViewIfNeeded({ timeout: 10_000 })
  const box = await locator.boundingBox()
  if (!box) throw new Error(`selector did not resolve to a visible box: ${selector}`)

  const size = page.viewportSize() ?? VIEWPORTS.desktop.viewport
  const padding = Number.isFinite(pad) ? pad : 48

  const x = Math.max(0, box.x - padding)
  const y = Math.max(0, box.y - padding)
  const width = Math.min(size.width - x, box.width + padding * 2)
  const height = Math.min(size.height - y, box.height + padding * 2)

  const coverage = (width * height) / (size.width * size.height)
  if (coverage >= CROP_COVERAGE_LIMIT) {
    return { clip: null, skipped: 'component fills most of the screen; kept the full view' }
  }
  return { clip: { x, y, width, height }, coverage: Number(coverage.toFixed(3)) }
}

function stamp(name, extension) {
  const safe = (name || 'shot').replace(/[^a-zA-Z0-9._-]/g, '-')
  const time = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19)
  return path.join(ARTIFACTS_DIR, `${time}-${safe}.${extension}`)
}

function shotLinks(result) {
  return Object.entries(result.files).map(([viewport, file]) => {
    const { kept, uploadError, ...links } = result.store?.[viewport] ?? {}
    if (kept === null) return { viewport, ...links }
    return { viewport, file, ...(uploadError ? { uploadError } : {}), ...(result.notes?.[viewport] ? { note: result.notes[viewport] } : {}) }
  })
}

function pickLinks(stored) {
  const links = {}
  for (const key of ['id', 'kind', 'caption', 'url', 'rawUrl', 'sessionUrl', 'downloadUrl']) {
    if (stored?.[key] !== undefined) links[key] = stored[key]
  }
  return links
}

async function storeUpload({ file, sid, kind, caption, tags, name }) {
  if (!sid) return { kept: file }
  try {
    const stored = await uploadArtifact({ file, sid, kind, caption: caption || name, tags })
    fs.rmSync(file, { force: true })
    return { kept: null, ...pickLinks(stored) }
  } catch (error) {
    return { kept: file, uploadError: error?.message ?? String(error) }
  }
}

const handlers = {
  async status() {
    const page = activePage()
    const extra = {}
    for (const ext of EXTENSIONS) if (ext.status) extra[ext.id] = await ext.status().catch((error) => ({ error: error.message }))
    return {
      up: Boolean(session.browser?.isConnected()),
      chrome: session.cdpUrl,
      webBaseUrl: WEB_BASE_URL,
      bypassCsp: BYPASS_CSP,
      url: page ? page.url() : null,
      sid: session.sid,
      extensions: EXTENSIONS.map((ext) => ext.id),
      ...(Object.keys(extra).length ? { extensionStatus: extra } : {}),
      recording: session.recording
        ? { name: session.recording.name, viewport: session.recording.which, frames: session.recording.frames.length,
            interactions: session.recording.interactions.length }
        : null,
      viewports: {
        desktop: VIEWPORTS.desktop.label,
        mobile: VIEWPORTS.mobile.label,
        mobileContextOpen: Boolean(session.mobile),
      },
      artifactsDir: ARTIFACTS_DIR,
      counts: {
        console: session.console.length,
        pageErrors: session.pageErrors.length,
        failedRequests: session.failedRequests.length,
        wsFrames: session.wsFrames.length,
      },
    }
  },

  async goto({ path: target }) {
    return { url: await gotoPath(target) }
  },

  async click(body) {
    const page = activePage()
    const matchCount = await locatorCount(page, body).catch(() => null)
    const popup = page.context().waitForEvent('page', { timeout: 1_500 }).catch(() => null)
    const locator = resolveLocator(page, body)
    let newTab = null
    if (body.real) {
      await locator.click({ timeout: body.timeout ?? 15_000 })
    } else {
      await releaseHover(page)
      newTab = (await inPage(page, locator, 'click', { hitTest: !body.force }, { timeout: body.timeout ?? 15_000 })).newTab
    }
    await settle(page, { timeout: 10_000 })
    const opened = await popup
    if (opened) {
      instrument(opened)
      await opened.close().catch(() => {})
    }
    persistSession()
    return {
      url: page.isClosed() ? null : page.url(),
      matchCount,
      popupClosed: opened ? opened.url() : undefined,
      newTab: newTab && !opened ? newTab : undefined,
    }
  },

  async hover(body) {
    const page = activePage()
    const locator = resolveLocator(page, body)
    if (body.real) {
      await locator.hover({ timeout: body.timeout ?? 15_000 })
      return { ok: true }
    }
    const outcome = await hoverInPage(page, locator, body.timeout ?? 15_000)
    return { ok: true, forcedHover: outcome.forcedHover }
  },

  async wheel(body) {
    const page = activePage()
    const locator = resolveLocator(page, body)
    if (body.real) {
      await locator.hover({ timeout: body.timeout ?? 15_000 })
      await page.mouse.wheel(body.deltaX ?? 0, body.deltaY ?? 400)
      return { ok: true }
    }
    const { box, done, ...moved } = await wheelInPage(page, locator, body.deltaX ?? 0, body.deltaY ?? 400, body.timeout ?? 15_000)
    return { ok: true, ...moved }
  },

  async fill(body) {
    const page = activePage()
    const locator = resolveLocator(page, body)
    if (body.real) await locator.fill(body.value ?? '')
    else await inPage(page, locator, 'fill', { value: String(body.value ?? '') }, { timeout: body.timeout ?? 15_000 })
    return { ok: true }
  },

  async press({ key, real }) {
    const page = activePage()
    if (real) {
      await page.keyboard.press(key)
      return { ok: true }
    }
    const outcome = await pressInPage(page, key)
    return { ok: true, target: outcome.target, action: outcome.action ?? undefined }
  },

  async waitFor({ text, selector, timeout }) {
    const page = activePage()
    if (text) await page.getByText(text, { exact: false }).first().waitFor({ timeout: timeout ?? 15_000 })
    else if (selector) await page.locator(selector).first().waitFor({ timeout: timeout ?? 15_000 })
    else await page.waitForLoadState('networkidle')
    return { ok: true }
  },

  async evaluate({ expression }) {
    return { result: await activePage().evaluate(expression) }
  },

  async text({ selector }) {
    const page = activePage()
    const target = selector ? page.locator(selector).first() : page.locator('body')
    return { text: (await target.innerText()).slice(0, 20_000) }
  },

  async shot({ name, fullPage, selector, viewport, focus, pad, here, hide, sid, caption, tags }) {
    const wanted = viewport ? [viewport] : ['desktop', 'mobile']
    const shots = {}
    const notes = {}
    const store = {}
    const shotHide = hideSelectorsFor(hide)

    for (const which of wanted) {
      let page
      if (which === 'mobile' && here) {
        const file = stamp(`${name || 'shot'}-${VIEWPORTS.mobile.label}`, 'png')
        page = activePage()
        await setPointerVisible(page, false)
        await applyHide(page, shotHide)
        try {
          await shotHere(page, file)
        } finally {
          await setPointerVisible(page, true)
          await clearHide(page)
        }
        notes.mobile = `captured from the current desktop page state at ${VIEWPORTS.mobile.label} metrics (here)`
        shots.mobile = file
        continue
      }
      if (which === 'mobile') {
        const mobile = await ensureMobile()
        page = mobile.page
        const auth = page === session.recording?.page ? null : await mirrorDesktopStorage(page).catch(() => null)
        const target = activePage().url()
        if (target && (page.url() !== target || auth?.changed)) {
          await page.goto(target, { waitUntil: 'domcontentloaded' }).catch(() => {})
          await settle(page)
        }
      } else {
        page = activePage()
      }

      const file = stamp(`${name || 'shot'}-${VIEWPORTS[which].label}`, 'png')

      await setPointerVisible(page, false)
      await applyHide(page, shotHide)
      try {
        if (selector) {
          await page.locator(selector).first().screenshot({ path: file })
        } else if (focus && which === 'desktop') {
          const { clip, skipped, coverage } = await focusClip(page, focus, pad)
          if (clip) {
            await page.screenshot({ path: file, clip })
            notes[which] = `cropped to ${focus} with context (${coverage} of viewport)`
          } else {
            await page.screenshot({ path: file, fullPage: Boolean(fullPage) })
            notes[which] = skipped
          }
        } else {
          if (focus && which === 'mobile') notes[which] = 'mobile is never cropped'
          await page.screenshot({ path: file, fullPage: Boolean(fullPage) })
        }
      } finally {
        await setPointerVisible(page, true)
        await clearHide(page)
      }

      shots[which] = file
    }

    for (const [which, file] of Object.entries(shots)) {
      const uploaded = await storeUpload({ file, sid, kind: 'screenshot', caption, tags, name: `${name || 'shot'}-${which}` })
      store[which] = uploaded
      if (uploaded.kept === null) shots[which] = uploaded.rawUrl ?? uploaded.url ?? file
      if (uploaded.uploadError) notes[which] = [notes[which], `store upload failed, local file kept: ${uploaded.uploadError}`].filter(Boolean).join(' ')
    }

    return { files: shots, notes, file: shots.desktop ?? shots.mobile, store }
  },

  async videoStart({ name, viewport, focus, pad, pace, fromHere, noPointer, hide, loggedOut }) {
    if (session.recording) throw new Error('already recording - stop it first')
    if (loggedOut && fromHere) throw new Error('--logged-out opens the login page, so it cannot be combined with --from-here')
    const resolved = parsePace(pace)
    await ensureBrowser()
    const which = viewport === 'mobile' ? 'mobile' : 'desktop'
    let page = session.page
    const notes = []
    if (which === 'mobile') {
      page = (await ensureMobile()).page
      const auth = loggedOut ? null : await mirrorDesktopStorage(page).catch(() => null)
      if (auth?.changed) notes.push('the phone page took over the sign-in state of the desktop page')
      const target = session.page.url()
      if (!loggedOut && target && (page.url() !== target || auth?.changed)) {
        await page.goto(target, { waitUntil: 'domcontentloaded' }).catch(() => {})
        await settle(page, { timeout: 10_000 })
        notes.push('the phone page follows URLs only; client-side state from the desktop page is not in this recording')
      }
    }
    if (loggedOut) await signOutPage(page)
    for (const ext of EXTENSIONS) if (ext.onRecordingStart) await ext.onRecordingStart(page).catch(() => {})
    if (!fromHere) {
      for (let attempt = 0; attempt < 10; attempt++) {
        const open = await page.evaluate(
          () => document.querySelectorAll('[role=dialog],[aria-modal="true"]').length).catch(() => 0)
        if (!open) break
        await pressInPage(page, 'Escape').catch(() => {})
        await page.waitForTimeout(100)
      }
      const homeUrl = new URL(HOME_PATH, WEB_BASE_URL + '/').href
      if (page.url() !== homeUrl || loggedOut) {
        await page.goto(homeUrl, { waitUntil: 'domcontentloaded' }).catch(() => {})
        notes.push('recording starts from the home route')
      }
      await settle(page, { timeout: 10_000 })
      if (loggedOut) notes.push(`recording starts signed out at ${page.url()} (storage and cookies of this page cleared; log in again with login afterwards)`)
      await paceSleep(Number(opt('videoLeadMs', 250)))
    } else {
      notes.push('recording starts here (--from-here): the opening frame is whatever was on screen')
    }
    const profile = VIEWPORTS[which]
    const dpr = profile.deviceScaleFactor
    const size = page.viewportSize() ?? profile.viewport
    const dir = fs.mkdtempSync(path.join(STATE_DIR, 'rec-'))
    const cdp = await page.context().newCDPSession(page)
    const rec = {
      page, cdp, dir, which, dpr, crop: null, frames: [], interactions: [],
      name: name || 'recording', startedAt: Date.now(), firstFrameMs: null,
      pace: resolved, paceName: resolved.name, fromHere: Boolean(fromHere),
      noPointer: Boolean(noPointer), hide: hideSelectorsFor(hide).filter((selector) => selector !== '#ehx-pointer-dot'), stepEnrich: [],
    }
    if (rec.noPointer) await setPointerVisible(page, false)
    await applyHide(page, rec.hide)
    await ensureAnnotations(page)
    await page.evaluate((on) => { window.__ehxRipple = on }, !rec.noPointer).catch(() => {})
    rec.onLoad = () => {
      if (session.recording !== rec) return
      ;(async () => {
        await applyHide(page, rec.hide)
        if (rec.noPointer) await setPointerVisible(page, false)
        await page.evaluate((on) => { window.__ehxRipple = on }, !rec.noPointer).catch(() => {})
        if (rec.visibleH) await page.evaluate((h) => document.documentElement.style.setProperty('--ehx-vh', h + 'px'), rec.visibleH).catch(() => {})
        await reapplyAnnotations(page)
      })().catch(() => {})
    }
    page.on('domcontentloaded', rec.onLoad)
    const addFrame = (buffer, seconds) => {
      const file = path.join(dir, `f${String(rec.frames.length).padStart(6, '0')}.jpg`)
      fs.writeFileSync(file, buffer)
      rec.frames.push({ file, t: seconds })
      if (rec.firstFrameMs === null) rec.firstFrameMs = Date.now() - rec.startedAt
    }
    rec.addFrame = addFrame
    cdp.on('Page.screencastFrame', ({ data, metadata, sessionId }) => {
      const visible = Math.round(Number(metadata?.deviceHeight) || 0)
      if (visible > 0 && visible !== rec.visibleH) {
        rec.visibleH = visible
        page.evaluate((h) => document.documentElement.style.setProperty('--ehx-vh', h + 'px'), visible).catch(() => {})
      }
      try {
        addFrame(Buffer.from(data, 'base64'), frameTime(metadata))
      } catch (error) {
        log(`screencast frame dropped: ${error.message}`)
      }
      cdp.send('Page.screencastFrameAck', { sessionId }).catch(() => {})
    })
    await page.bringToFront().catch(() => {})
    session.recording = rec
    try {
      await withTimeout(cdp.send('Page.startScreencast', {
        format: 'jpeg', quality: 85, everyNthFrame: 1,
        maxWidth: Math.round(size.width * dpr), maxHeight: Math.round(size.height * dpr),
      }), CDP_CAPTURE_TIMEOUT_MS, 'Page.startScreencast')
    } catch (error) {
      session.recording = null
      await cdp.detach().catch(() => {})
      fs.rmSync(dir, { recursive: true, force: true })
      throw error
    }
    const initial = await withTimeout(cdp.send('Page.captureScreenshot', { format: 'jpeg', quality: 85 }),
      CDP_CAPTURE_TIMEOUT_MS, 'Page.captureScreenshot').catch(() => null)
    if (initial && rec.frames.length === 0) addFrame(Buffer.from(initial.data, 'base64'), Date.now() / 1000)

    if (focus && which === 'desktop') {
      try {
        const { clip, skipped } = await focusClip(page, focus, pad)
        rec.crop = clip
        notes.push(clip ? `will crop to ${focus} with context` : skipped)
      } catch (error) {
        notes.push(`focus ignored: ${error.message}`)
      }
    } else if (focus) {
      notes.push('mobile is never cropped')
    }
    return {
      recording: rec.name,
      viewport: profile.label,
      source: 'live page (CDP screencast)',
      url: page.url(),
      firstFrameMs: rec.firstFrameMs,
      pace: rec.paceName,
      fromHere: rec.fromHere,
      loggedOut: Boolean(loggedOut),
      notes,
    }
  },

  async videoStop({ trim, sid, caption, tags, name, motion: checkMotion } = {}) {
    if (!session.recording) throw new Error('not recording')
    const rec = session.recording
    if (rec.onLoad) rec.page.off('domcontentloaded', rec.onLoad)
    keptAnnotations.clear()
    const annotated = Boolean(rec.annotations?.length)
    if (trim === undefined || trim === null || trim === '') trim = !annotated
    if (trim === 'false' || trim === '0') trim = false
    session.recording = null
    if (!rec.page.isClosed()) await hidePressMark(rec.page)
    await withTimeout(rec.cdp.send('Page.stopScreencast'), CDP_CAPTURE_TIMEOUT_MS, 'Page.stopScreencast').catch(() => {})
    const stopAt = Date.now() / 1000
    const last = await withTimeout(rec.cdp.send('Page.captureScreenshot', { format: 'jpeg', quality: 85 }),
      CDP_CAPTURE_TIMEOUT_MS, 'Page.captureScreenshot').catch(() => null)
    if (last) rec.addFrame(Buffer.from(last.data, 'base64'), stopAt)
    await rec.cdp.detach().catch(() => {})
    if (!rec.page.isClosed()) {
      await setPointerVisible(rec.page, true)
      await clearHide(rec.page)
      await rec.page.evaluate(() => { window.__ehxRipple = false; window.__ehxAnno?.clear(); document.documentElement.style.removeProperty('--ehx-vh') }).catch(() => {})
    }

    const frames = rec.frames.sort((x, y) => x.t - y.t)
    if (frames.length === 0) throw new Error('no frames were captured')
    await Promise.allSettled((rec.stepEnrich ?? []).splice(0))
    const list = path.join(rec.dir, 'frames.tsv')
    fs.writeFileSync(list, frames.map((frame) => `${frame.t.toFixed(4)}\t${frame.file}`).join('\n') + '\n')
    const stepsFile = path.join(rec.dir, 'steps.jsonl')
    const clipOffset = frames[0].t - rec.startedAt / 1000
    fs.writeFileSync(stepsFile, rec.interactions.map((entry) => JSON.stringify({
      t: Math.max(0, Number((entry.at - clipOffset).toFixed(3))),
      action: entry.action,
      target: entry.target && typeof entry.target === 'object' && !Array.isArray(entry.target)
        ? entry.target : { args: entry.target },
      page: entry.page ?? rec.which,
      ok: entry.ok !== false,
      ...(entry.error ? { error: String(entry.error).slice(0, 300) } : {}),
      ...(entry.ms !== undefined ? { ms: entry.ms } : {}),
      ...(entry.boxes ? { boxes: entry.boxes } : {}),
    })).join('\n') + '\n')
    const total = frames[frames.length - 1].t - frames[0].t

    const out = stamp(`${rec.name}-${VIEWPORTS[rec.which].label}`, 'mp4')
    const args = ['--timed-frames', list, '--out', out]
    if (trim) args.push('--trim', '--step-log', stepsFile)
    if (rec.crop) {
      const c = rec.crop
      const w = Math.floor((c.width * rec.dpr) / 2) * 2
      const h = Math.floor((c.height * rec.dpr) / 2) * 2
      args.push('--crop', `${w}:${h}:${Math.floor(c.x * rec.dpr)}:${Math.floor(c.y * rec.dpr)}`)
    }
    const timings = { frames: frames.length }
    let mark = Date.now()
    const encoded = runEncoder(args)
    timings.encodeMs = Date.now() - mark
    if (encoded.status !== 0) {
      const exit = encoded.error ? encoded.error.message
        : encoded.signal ? `killed by ${encoded.signal}` : `exit code ${encoded.status}`
      const detail = [encoded.stderr, encoded.stdout].map((text) => String(text ?? '').trim()).filter(Boolean).join('\n').slice(-1200)
      const message = `video encoding failed (${exit}, ${frames.length} frames over ${total.toFixed(1)}s): ${detail || 'the encoder printed nothing'}; frames kept in ${rec.dir}`
      log(`${message}\n  command: ${ENCODER.join(' ')} ${args.join(' ')}`)
      throw new Error(message)
    }
    fs.rmSync(rec.dir, { recursive: true, force: true })
    let [file, trimmed] = encoded.stdout.trim().split('\n')

    const failed = rec.interactions.filter((entry) => !entry.ok)
    mark = Date.now()
    const motion = (checkMotion ?? !annotated) && checkMotion !== 'false'
      ? looksStatic(file, total)
      : { static: false, checked: false, reason: annotated ? 'annotated recording: motion check skipped' : 'motion check off' }
    const warnings = []
    if (rec.interactions.length === 0) warnings.push('no harness interactions happened between video start and video stop')
    if (failed.length) warnings.push(`${failed.length} interaction(s) failed during the recording - see the sidecar log`)
    if (motion.static) warnings.push('hint: no visible change detected after the first 2 seconds - check the frames before assuming the flow failed')
    const sidecar = file.replace(/\.mp4$/, '.interactions.json')
    fs.writeFileSync(sidecar, JSON.stringify({
      video: file,
      trimmed: trimmed ?? null,
      viewport: VIEWPORTS[rec.which].label,
      durationSeconds: Number(total.toFixed(2)),
      firstFrameMs: rec.firstFrameMs,
      pace: rec.paceName ?? 'demo',
      fromHere: Boolean(rec.fromHere),
      screencastFrames: frames.length,
      motion,
      interactions: rec.interactions,
      annotations: rec.annotations ?? [],
      warnings,
    }, null, 2))
    if (warnings.length) log(`video ${file}: ${warnings.join(' | ')}`)
    timings.motionMs = Date.now() - mark
    mark = Date.now()
    const store = {}
    const clipName = name || rec.name
    const baseTags = String(tags ?? '').split(',').map((tag) => tag.trim()).filter(Boolean)
    store.video = await storeUpload({
      file: trimmed ?? file, sid, kind: 'video', caption,
      tags: trimmed ? [...baseTags, 'trimmed'] : baseTags, name: clipName,
    })
    if (trimmed && trimmed !== file) {
      store.full = await storeUpload({ file, sid, kind: 'video', caption, tags: [...baseTags, 'full'], name: `${clipName}-full` })
      if (store.full.kept === null) file = store.full.rawUrl ?? store.full.url ?? file
    } else if (store.video.kept === null) {
      file = store.video.rawUrl ?? store.video.url ?? file
    }
    timings.uploadMs = Date.now() - mark
    log(`video stop ${clipName}: ${JSON.stringify(timings)}`)
    return {
      file,
      timings,
      trimmed: trimmed ?? null,
      durationSeconds: Number(total.toFixed(2)),
      encoding: encoded.stderr.trim().split('\n'),
      firstFrameMs: rec.firstFrameMs,
      interactionsDuringRecording: rec.interactions.length,
      failedInteractions: failed,
      interactionsLog: sidecar,
      annotations: rec.annotations ?? [],
      motion,
      warnings,
      store,
    }
  },

  async dump({ what, name, viewport = 'desktop', sid, caption, tags }) {
    if (!['dom', 'mhtml', 'a11y'].includes(what)) throw new Error(`dump must be dom, mhtml or a11y (got ${JSON.stringify(what)})`)
    let page
    if (viewport === 'mobile') {
      const mobile = await ensureMobile()
      page = mobile.page
      const target = activePage().url()
      if (target && page.url() !== target) {
        await page.goto(target, { waitUntil: 'domcontentloaded' }).catch(() => {})
        await settle(page)
      }
    } else {
      page = activePage()
    }
    const base = name || what
    let file
    if (what === 'dom') {
      file = stamp(`${base}-${viewport}`, 'html')
      fs.writeFileSync(file, await page.content())
    } else if (what === 'mhtml') {
      const cdp = await page.context().newCDPSession(page)
      try {
        const snapshot = await cdp.send('Page.captureSnapshot', { format: 'mhtml' })
        file = stamp(`${base}-${viewport}`, 'mht')
        fs.writeFileSync(file, snapshot.data ?? '')
      } finally {
        await cdp.detach().catch(() => {})
      }
    } else {
      file = stamp(`${base}-${viewport}`, 'txt')
      fs.writeFileSync(file, await ariaTree(page, null))
    }
    const store = await storeUpload({ file, sid, kind: what, caption, tags, name: base })
    if (store.kept === null) file = store.rawUrl ?? store.url ?? file
    return { file, kind: what, viewport, store }
  },

  async harStart() {
    if (session.har) throw new Error('har is already recording - run har stop first')
    const page = activePage()
    const cdp = await page.context().newCDPSession(page)
    const rec = { cdp, url: page.url(), startedAt: new Date().toISOString(), order: [], byId: new Map() }
    const touch = (id) => {
      if (!rec.byId.has(id)) {
        const entry = { id, order: rec.order.length }
        rec.byId.set(id, entry)
        rec.order.push(entry)
      }
      return rec.byId.get(id)
    }
    cdp.on('Network.requestWillBeSent', (event) => {
      const entry = touch(event.requestId)
      entry.started = event.timestamp
      entry.wallTime = event.wallTime
      entry.request = { method: event.request.method, url: event.request.url, headers: event.request.headers }
    })
    cdp.on('Network.responseReceived', (event) => {
      const entry = touch(event.requestId)
      entry.received = event.timestamp
      entry.response = { status: event.response.status, mimeType: event.response.mimeType, headers: event.response.headers }
    })
    cdp.on('Network.loadingFinished', (event) => {
      const entry = touch(event.requestId)
      entry.finished = event.timestamp
      entry.size = event.encodedDataLength
    })
    cdp.on('Network.loadingFailed', (event) => {
      const entry = touch(event.requestId)
      entry.finished = event.timestamp
      entry.failed = event.errorText
    })
    await cdp.send('Network.enable')
    session.har = rec
    return { recording: true, page: rec.url }
  },

  async harStop({ name, whole = false, sid, caption, tags } = {}) {
    if (whole && !session.har) throw new Error('whole-session har needs har start first: run har start, drive, then har stop')
    const rec = session.har
    if (!rec) throw new Error('har is not recording - run har start first')
    session.har = null
    await rec.cdp.detach().catch(() => {})
    const entries = rec.order
      .filter((entry) => entry.request)
      .map((entry) => ({
        startedDateTime: new Date(entry.wallTime * 1000).toISOString(),
        time: Math.max(0, ((entry.finished ?? entry.received ?? entry.started) - entry.started) * 1000),
        request: {
          method: entry.request.method, url: entry.request.url, httpVersion: 'HTTP/1.1',
          headers: Object.entries(entry.request.headers ?? {}).map(([n, v]) => ({ name: n, value: String(v) })),
          queryString: [], cookies: [], headersSize: -1, bodySize: -1,
        },
        response: {
          status: entry.response?.status ?? 0, statusText: entry.failed ? 'failed' : '',
          httpVersion: 'HTTP/1.1',
          headers: Object.entries(entry.response?.headers ?? {}).map(([n, v]) => ({ name: n, value: String(v) })),
          cookies: [], content: { size: entry.size ?? 0, mimeType: entry.response?.mimeType ?? '' },
          redirectURL: '', headersSize: -1, bodySize: entry.size ?? 0,
          ...(entry.failed ? { _failed: entry.failed } : {}),
        },
        cache: {}, timings: { send: 0, wait: 0, receive: 0 },
      }))
    const har = {
      log: {
        version: '1.2', creator: { name: 'eks-harness', version: '1' },
        pages: [{ startedDateTime: rec.startedAt, id: 'page', title: rec.url }],
        entries,
      },
    }
    let file = stamp(`${name || 'har'}`, 'har')
    fs.writeFileSync(file, JSON.stringify(har))
    const store = await storeUpload({ file, sid, kind: 'har', caption, tags, name: name || 'har' })
    if (store.kept === null) file = store.rawUrl ?? store.url ?? file
    return { file, entries: entries.length, page: rec.url, store }
  },

  async consoleExcerpt({ since, name, sid, caption, tags } = {}) {
    let cutoff = 0
    if (since) {
      const relative = String(since).match(/^(\d+)([smh])$/)
      cutoff = relative
        ? Date.now() - Number(relative[1]) * { s: 1000, m: 60_000, h: 3_600_000 }[relative[2]]
        : Date.parse(since)
      if (!Number.isFinite(cutoff)) throw new Error(`since must be like '5m' or an ISO time (got ${JSON.stringify(since)})`)
    }
    const rows = []
    for (const [source, buffer] of [['console', session.console], ['pageerror', session.pageErrors], ['failed-request', session.failedRequests]]) {
      for (const entry of buffer) {
        const at = entry?.at ? Date.parse(entry.at) : 0
        if (at >= cutoff) rows.push({ at, source, entry })
      }
    }
    rows.sort((a, b) => a.at - b.at)
    const lines = rows.slice(-2000).map(({ source, entry }) => `[${entry.at ?? '?'}] [${source}] ${typeof entry === 'string' ? entry : JSON.stringify(entry)}`)
    let file = stamp(`${name || 'console'}`, 'log')
    fs.writeFileSync(file, lines.join('\n') + '\n')
    const store = await storeUpload({ file, sid, kind: 'log', caption, tags, name: name || 'console' })
    if (store.kept === null) file = store.rawUrl ?? store.url ?? file
    return { file, lines: lines.length, store }
  },

  async logs({ kind = 'console', limit = 100 }) {
    const source =
      kind === 'errors' ? session.pageErrors
        : kind === 'network' ? session.failedRequests
          : kind === 'ws' ? session.wsFrames
            : session.console
    return { kind, entries: source.slice(-Number(limit)) }
  },

  async clearLogs() {
    session.console.length = 0
    session.pageErrors.length = 0
    session.failedRequests.length = 0
    session.wsFrames.length = 0
    return { ok: true }
  },

  async down() {
    setTimeout(async () => {
      try {
        if (session.har) await session.har.cdp.detach().catch(() => {})
        if (session.recording) await session.recording.cdp.send('Page.stopScreencast').catch(() => {})
        if (session.mobile) await session.mobile.context.close()
        await session.browser?.close()
      } finally {
        process.exit(0)
      }
    }, 50)
    return { stopping: true }
  },
}
const TRACKED = new Set(['click', 'hover', 'wheel', 'fill', 'press', 'waitFor', 'evaluate', 'goto'])
const LOCATOR_ACTIONS = new Set(['click', 'hover', 'wheel', 'fill'])

function describeTarget(body) {
  const target = {}
  for (const key of ['selector', 'text', 'testId', 'role', 'name', 'path', 'key']) {
    if (body[key] !== undefined) target[key] = body[key]
  }
  if (body.expression !== undefined) target.expression = String(body.expression).slice(0, 200)
  if (body.value !== undefined) target.valueLength = String(body.value).length
  return target
}

async function locatorCount(page, { selector, text, testId, role, name }) {
  if (testId) return page.getByTestId(testId).count()
  if (role) return page.getByRole(role, name ? { name } : undefined).count()
  if (text) return page.getByText(text, { exact: false }).count()
  if (selector) return page.locator(selector).count()
  return null
}

const ROUTES = {
  '/status': 'status',
  '/goto': 'goto',
  '/click': 'click',
  '/hover': 'hover',
  '/wheel': 'wheel',
  '/fill': 'fill',
  '/press': 'press',
  '/wait-for': 'waitFor',
  '/eval': 'evaluate',
  '/text': 'text',
  '/shot': 'shot',
  '/video/start': 'videoStart',
  '/video/stop': 'videoStop',
  '/dump': 'dump',
  '/har/start': 'harStart',
  '/har/stop': 'harStop',
  '/console': 'consoleExcerpt',
  '/logs': 'logs',
  '/logs/clear': 'clearLogs',
  '/down': 'down',
}

const TREE_MAX_LINES = Number(opt('treeMaxLines', 160))
const APP_ORIGINS = [new URL(WEB_BASE_URL).origin, API_BASE_URL ? new URL(API_BASE_URL).origin : null, ...(CONFIG.appOrigins ?? [])].filter(Boolean)
const isAppUrl = (url) => APP_ORIGINS.some((origin) => String(url ?? '').startsWith(origin))

const CSS_SHAPE = /^[#.[]|^[a-z][a-z0-9-]*(\[|\.[a-z_-]|#[a-z]|:(?:not|has|nth|first|last|is)|\s*>)/i
const HTML_TAGS = new Set(['main', 'header', 'footer', 'nav', 'section', 'article', 'aside', 'form', 'table', 'tbody', 'thead', 'tr', 'td', 'th', 'ul', 'ol', 'li', 'input', 'button', 'select', 'textarea', 'dialog', 'label', 'h1', 'h2', 'h3', 'h4', 'body'])

function looksLikeCss(text) {
  const value = text.trim()
  if (value.split(/\s+/).every((part) => HTML_TAGS.has(part.toLowerCase()))) return true
  if (/\s{2,}|[.!?]$/.test(value)) return false
  return CSS_SHAPE.test(value)
}

async function present(page, target, timeout = 3_000) {
  const locator = locate(page, target, { visibleOnly: false })
  const start = Date.now()
  while (Date.now() - start < timeout) {
    if ((await locator.count().catch(() => 0)) > 0) return resolveTarget(page, target)
    await page.waitForTimeout(60)
  }
  const hints = typeof target === 'string' ? await nearMisses(page, target) : []
  const suggestion = hints.length ? `; visible controls with similar text: ${hints.map((hint) => JSON.stringify(hint)).join(', ')}` : ''
  throw new Error(`nothing on ${page.url()} matches ${JSON.stringify(target)} (checked for ${timeout}ms; bare strings are visible text unless they look like CSS, 'css=' forces a selector)${suggestion}`)
}

async function nearMisses(page, target) {
  const words = target.toLocaleLowerCase(LOCALE).split(/[^\p{L}\p{N}]+/u).filter((word) => word.length >= 3)
  if (!words.length) return []
  return page.evaluate((needles) => {
    const found = []
    const selector = 'button, a, [role=button], [role=menuitem], [role=menuitemradio], [role=menuitemcheckbox], [role=option], [role=tab], [role=combobox], input, label'
    for (const el of document.querySelectorAll(selector)) {
      const box = el.getBoundingClientRect()
      if (!box.width || !box.height) continue
      const text = (el.getAttribute('aria-label') || el.innerText || el.getAttribute('placeholder') || '').replace(/\s+/g, ' ').trim()
      if (!text || text.length > 80) continue
      const lower = text.toLocaleLowerCase(LOCALE)
      if (needles.some((needle) => lower.includes(needle))) {
        const role = el.getAttribute('role') || el.tagName.toLowerCase()
        const entry = `${role}: ${text}`
        if (!found.includes(entry)) found.push(entry)
      }
      if (found.length >= 8) break
    }
    return found
  }, words).catch(() => [])
}

function targetSpec(target) {
  if (typeof target !== 'string') return target
  const [, kind, rest] = target.match(/^(css|role|label|placeholder|text|testid)=(.*)$/s) ?? []
  if (kind === 'css') return { selector: rest }
  if (kind === 'role') {
    const [role, ...name] = rest.split(':')
    return { role, name: name.join(':') || undefined }
  }
  if (kind === 'label') return { label: rest }
  if (kind === 'placeholder') return { placeholder: rest }
  if (kind === 'testid') return { testId: rest }
  if (kind === 'text') return { text: rest }
  if (/^#[\w-]+$/.test(target)) return { idOrTestId: target.slice(1) }
  if (looksLikeCss(target)) return { selector: target }
  return { text: target }
}

function roleName(spec, exact) {
  if (!spec.name) return undefined
  if (spec.nameMatch === 'exact') return { name: spec.name, exact: true }
  if (spec.nameMatch === 'ci') {
    const escaped = String(spec.name).trim().replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
    return { name: new RegExp(`^\\s*${escaped}\\s*$`, 'i') }
  }
  return { name: spec.name, exact }
}

function locate(page, target, { visibleOnly = true, all = false } = {}) {
  const spec = targetSpec(target)
  const root = spec.within ? locate(page, spec.within, { visibleOnly: false }) : page
  const exact = spec.exact ?? false
  let locator
  if (spec.idOrTestId) {
    const id = spec.idOrTestId.replace(/"/g, '\\"')
    locator = root.locator(`[data-testid="${id}"], [id="${id}"]`)
  } else if (spec.testId) locator = root.getByTestId(spec.testId)
  else if (spec.role) locator = root.getByRole(spec.role, roleName(spec, exact))
  else if (spec.label) locator = root.getByLabel(spec.label, { exact })
  else if (spec.placeholder) locator = root.getByPlaceholder(spec.placeholder, { exact })
  else if (spec.text) locator = root.getByText(spec.text, { exact })
  else if (spec.selector) locator = root.locator(spec.selector)
  else throw new Error(`cannot resolve target ${JSON.stringify(target)}`)
  if (visibleOnly) locator = locator.filter({ visible: true })
  if (all) return locator
  return spec.nth !== undefined ? locator.nth(spec.nth) : locator.first()
}

async function resolveTarget(page, target) {
  let spec
  try { spec = targetSpec(target) } catch { return target }
  if (!spec?.role || !spec.name || spec.exact !== undefined || spec.nameMatch) return target
  for (const nameMatch of ['exact', 'ci']) {
    if (await locate(page, { ...spec, nameMatch }, { all: true }).count().catch(() => 0)) return { ...spec, nameMatch }
  }
  const loose = locate(page, { ...spec, nameMatch: 'substring' }, { all: true })
  const matches = await loose.count().catch(() => 0)
  if (matches > 1 && spec.nth === undefined) {
    const names = await loose.evaluateAll((els) => els.slice(0, 12).map((el) =>
      (el.getAttribute('aria-label') || el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim())).catch(() => [])
    throw new Error(`no ${spec.role} on ${page.url()} is named exactly ${JSON.stringify(spec.name)} and ${matches} contain it: ${names.map((name) => JSON.stringify(name)).join(', ')}; pass the full name, exact: false with nth, or a testId`)
  }
  return { ...spec, nameMatch: 'substring' }
}

async function codeClick(page, target, { real = false, force = false, timeout = 10_000, settleAfter = true, wait = 3_000, count = 1 } = {}) {
  const resolved = await present(page, target, wait)
  const locator = locate(page, resolved)
  if (real) {
    if (count === 2) await locator.dblclick({ timeout })
    else await locator.click({ timeout })
  } else {
    await releaseHover(page)
    await inPage(page, locator, 'click', { hitTest: !force, count }, { timeout })
  }
  if (settleAfter) await settle(page)
  return true
}

function compactAria(yaml) {
  const out = []
  let skipDeeperThan = -1
  for (const line of yaml.split('\n')) {
    const indent = line.length - line.trimStart().length
    if (skipDeeperThan >= 0) {
      if (indent > skipDeeperThan) continue
      skipDeeperThan = -1
    }
    const text = line.trim()
    if (text.startsWith('- /url:')) continue
    if (/^- row "/.test(text)) {
      out.push(line.replace(/:$/, ''))
      skipDeeperThan = indent
      continue
    }
    out.push(line)
  }
  return out.join('\n')
}

async function ariaTree(page, target, maxLines = TREE_MAX_LINES) {
  const root = target ? locate(page, target) : page.locator('body')
  const yaml = compactAria(await root.ariaSnapshot({ timeout: 5_000 }))
  const lines = yaml.split('\n')
  if (lines.length <= maxLines) return yaml
  return `${lines.slice(0, maxLines).join('\n')}\n... ${lines.length - maxLines} more lines (tree('<target>') narrows it)`
}

async function pageSnapshot({ reason, shot, label } = {}, since = 0) {
  const page = activePage()
  const out = { url: page.url(), title: await page.title().catch(() => null) }
  const errors = session.pageErrors.filter((entry) => Date.parse(entry.at) >= since)
  const failed = session.failedRequests.filter((entry) => Date.parse(entry.at) >= since && isAppUrl(entry.url))
  const consoleErrors = session.console.filter((entry) => entry.type === 'error' && Date.parse(entry.at) >= since)
  if (errors.length) out.pageErrors = errors.slice(-5).map((entry) => entry.message ?? entry.text ?? JSON.stringify(entry))
  if (consoleErrors.length) out.consoleErrors = consoleErrors.slice(-5).map((entry) => String(entry.text).slice(0, 300))
  if (failed.length) out.failedRequests = failed.slice(-5).map((entry) => `${entry.status ?? ''} ${entry.method ?? ''} ${String(entry.url ?? '').slice(0, 200)} ${entry.failure ?? ''}`.trim())
  if (shot || reason === 'error') {
    const result = await handlers.shot({ name: label || reason || 'pause', viewport: shot === 'both' ? undefined : shot === 'mobile' ? 'mobile' : 'desktop', here: true })
    out.shots = shotLinks(result)
  }
  out.tree = await ariaTree(page).catch((error) => `(tree unavailable: ${error.message})`)
  return out
}

let runSince = Date.now()

function readMs(...texts) {
  const chars = texts.filter(Boolean).join(' ').length
  return Math.max(1600, Math.min(6000, 1000 + chars * 50))
}
let annoCounter = 0
function annoFind(spec) {
  if (!spec || typeof spec !== 'object') return null
  if (spec.idOrTestId) return { selector: `[data-testid="${spec.idOrTestId}"], [id="${spec.idOrTestId}"]` }
  if (spec.testId) return { selector: `[data-testid="${spec.testId}"]` }
  if (spec.selector) return { selector: spec.selector }
  if (spec.text) return { text: spec.text }
  if (spec.name) return { text: spec.name }
  if (spec.label) return { text: spec.label }
  return null
}
async function ensureAnnotations(page) {
  const ready = await page.evaluate(() => typeof window.__ehxAnno === 'object').catch(() => false)
  if (!ready) await page.evaluate(ANNOTATION_OVERLAY).catch(() => {})
}
async function annoMark(page, target, key) {
  const resolved = await present(page, target, 5_000)
  const locator = locate(page, resolved)
  await locator.scrollIntoViewIfNeeded({ timeout: 5_000 }).catch(() => {})
  await locator.evaluate((el, name) => {
    const keys = new Set((el.getAttribute('data-ehx-anno') || '').split(/\s+/).filter(Boolean))
    keys.add(name)
    el.setAttribute('data-ehx-anno', [...keys].join(' '))
  }, key, { timeout: 5_000 })
  let spec
  try { spec = targetSpec(resolved) } catch {}
  return annoFind(spec)
}
async function annoCall(page, method, args) {
  await ensureAnnotations(page)
  return page.evaluate(([name, value]) => window.__ehxAnno[name](value), [method, args])
}
function annoRecord(entry) {
  const rec = session.recording
  if (!rec) return
  ;(rec.annotations ??= []).push({ at: Number(((Date.now() - rec.startedAt) / 1000).toFixed(2)), ...entry })
}
const keptAnnotations = new Map()
async function reapplyAnnotations(page) {
  if (!keptAnnotations.size) return
  await ensureAnnotations(page)
  for (const [key, { method, args }] of keptAnnotations) {
    await page.evaluate(([name, value]) => window.__ehxAnno[name](value), [method, { ...args, key }]).catch(() => {})
  }
}
async function annoShow(page, { method, args, key, hold, keep, text }) {
  await annoCall(page, method, { ...args, key })
  if (keep) keptAnnotations.set(key, { method, args })
  annoRecord({ kind: method, key, text: text ?? args.text ?? args.title ?? null })
  const wait = hold ?? (session.recording ? readMs(text ?? args.text ?? args.title, args.subtitle) : 0)
  if (wait > 0) await paceSleep(wait)
  if (!keep) {
    keptAnnotations.delete(key)
    await annoCall(page, 'remove', key).catch(() => {})
    if (session.recording) await paceSleep(220)
  }
  return key
}
function annotationHelpers(withPage) {
  return {
    grouped: true,
    title: withPage(async (p, title, { subtitle, kicker, hold, keep = false, key = 'title' } = {}) =>
      annoShow(p, { method: 'title', args: { title, subtitle, kicker }, key, hold: hold ?? (session.recording ? Math.max(2200, readMs(title, subtitle)) : 0), keep, text: title })),
    caption: withPage(async (p, text, { step, position, hold, keep = true, key = 'caption' } = {}) => {
      if (text === null || text === undefined || text === '') {
        keptAnnotations.delete(key)
        await annoCall(p, 'remove', key)
        return null
      }
      return annoShow(p, { method: 'caption', args: { text, step, position }, key, hold: hold ?? (session.recording ? 900 : 0), keep, text })
    }),
    callout: withPage(async (p, target, text, { placement, hold, keep = false, key } = {}) => {
      const name = key ?? `c${++annoCounter}`
      const find = await annoMark(p, target, name)
      return annoShow(p, { method: 'callout', args: { text, placement, find }, key: name, hold, keep, text })
    }),
    highlight: withPage(async (p, target, { hold, keep = false, key } = {}) => {
      const name = key ?? `h${++annoCounter}`
      const find = await annoMark(p, target, name)
      return annoShow(p, { method: 'highlight', args: { find }, key: name, hold: hold ?? (session.recording ? 1600 : 0), keep })
    }),
    spotlight: withPage(async (p, target, { text, placement, hold, keep = false, key } = {}) => {
      const name = key ?? `s${++annoCounter}`
      const find = await annoMark(p, target, name)
      await annoCall(p, 'spotlight', { key: name, find })
      if (text) await annoCall(p, 'callout', { key: `${name}-text`, text, placement, find })
      annoRecord({ kind: 'spotlight', key: name, text: text ?? null })
      const wait = hold ?? (session.recording ? readMs(text) : 0)
      if (wait > 0) await paceSleep(wait)
      if (!keep) {
        await annoCall(p, 'remove', name).catch(() => {})
        if (text) await annoCall(p, 'remove', `${name}-text`).catch(() => {})
        if (session.recording) await paceSleep(220)
      }
      return name
    }),
    remove: withPage(async (p, key) => {
      keptAnnotations.delete(key)
      return annoCall(p, 'remove', key)
    }),
    clear: withPage(async (p, kind) => {
      for (const [key, entry] of [...keptAnnotations]) if (!kind || entry.method === kind) keptAnnotations.delete(key)
      return annoCall(p, 'clear', kind)
    }),
    list: withPage(async (p) => annoCall(p, 'list')),
  }
}

function scriptHelpers() {
  const page = activePage()
  const withPage = (fn) => (...args) => fn(activePage(), ...args)
  return {
    page,
    context: page.context(),
    goto: async (target) => gotoPath(target),
    nav: async (target) => {
      const current = activePage()
      const strategy = CONFIG.nav ?? 'location'
      const pushed = strategy === 'location' ? false : await current.evaluate(([href, how]) => {
        const router = how === 'next' ? window.next?.router : how === 'hook' ? window.__ehxNavigate && { push: window.__ehxNavigate } : null
        if (!router?.push) return false
        router.push(href)
        return true
      }, [target, strategy]).catch(() => false)
      if (!pushed) return gotoPath(target)
      const wanted = new URL(target, WEB_BASE_URL).pathname
      const moved = await current.waitForURL((url) => url.pathname === wanted, { timeout: 4_000 }).then(() => true, () => false)
      if (!moved) {
        log(`nav ${target}: client-side push did not move the URL, falling back to a full load`)
        return gotoPath(target)
      }
      await settle(current, { timeout: 15_000 })
      persistSession()
      return current.url()
    },
    reload: async () => {
      await activePage().reload({ waitUntil: 'domcontentloaded' })
      await settle(activePage(), { timeout: 15_000 })
      return activePage().url()
    },
    back: async () => {
      await activePage().goBack({ waitUntil: 'domcontentloaded' })
      await settle(activePage())
      return activePage().url()
    },
    click: withPage(async (p, target, options) => {
      const before = p.url()
      const resolved = await present(p, target, options?.wait)
      stampAction(await scriptLead(p, resolved))
      const done = await pressed(p, () => codeClick(p, resolved, options))
      await scriptHold(p, before)
      return done
    }),
    dblclick: withPage(async (p, target, options = {}) => {
      await codeClick(p, target, { ...options, count: 2 })
      return true
    }),
    hover: withPage(async (p, target, { real = false } = {}) => {
      const locator = locate(p, await resolveTarget(p, target))
      if (real) await locator.hover({ timeout: 10_000 })
      else await hoverInPage(p, locator, 10_000)
      return true
    }),
    wheel: withPage(async (p, target, { deltaX = 0, deltaY = 400, real = false } = {}) => {
      const locator = locate(p, await resolveTarget(p, target))
      if (real) {
        await locator.hover({ timeout: 10_000 })
        await p.mouse.wheel(deltaX, deltaY)
        return true
      }
      const { box, done, ...moved } = await wheelInPage(p, locator, deltaX, deltaY)
      await settle(p, { timeout: 5_000 })
      return moved
    }),
    fill: withPage(async (p, target, value, { wait = 3_000, real = false } = {}) => {
      const before = p.url()
      const resolved = await present(p, target, wait)
      stampAction(await scriptLead(p, resolved))
      const locator = locate(p, resolved)
      await pressed(p, () => (real
        ? locator.fill(String(value ?? ''), { timeout: 10_000 })
        : inPage(p, locator, 'fill', { value: String(value ?? '') })))
      await scriptHold(p, before)
      return true
    }),
    type: withPage(async (p, target, value, { delay = 0, wait = 3_000, real = false } = {}) => {
      const before = p.url()
      const resolved = await present(p, target, wait)
      const pace = recPace()
      const perChar = pace?.typeMsPerChar || delay
      if (pace?.typeMsPerChar) stampAction(await scriptLead(p, resolved))
      const locator = locate(p, resolved)
      await pressed(p, async () => {
        if (real) return locator.pressSequentially(String(value ?? ''), { delay: perChar, timeout: 60_000 })
        for (const character of Array.from(String(value ?? ''))) {
          await inPage(p, locator, 'type', { character })
          if (perChar) await paceSleep(perChar)
        }
      })
      if (pace?.typeMsPerChar) await scriptHold(p, before)
      await settle(p, { timeout: 5_000 })
      return true
    }),
    press: withPage(async (p, key, target, { real = false } = {}) => {
      const pace = recPace()
      if (pace?.dwellMs) await paceSleep(pace.dwellMs)
      stampAction()
      const locator = target ? locate(p, await resolveTarget(p, target)) : null
      if (real) {
        if (locator) await locator.press(key)
        else await p.keyboard.press(key)
      } else {
        await pressInPage(p, key, locator)
      }
      await settle(p, { timeout: 5_000 })
      if (pace) await scriptHold(p)
      return true
    }),
    select: withPage(async (p, target, value) => {
      const chosen = await locate(p, await resolveTarget(p, target)).selectOption(value, { timeout: 10_000 })
      await settle(p)
      return chosen
    }),
    check: withPage(async (p, target, on = true, { real = false, force = false } = {}) => {
      const locator = locate(p, await resolveTarget(p, target))
      if (real) await locator.setChecked(on, { timeout: 10_000, force: true })
      else await inPage(p, locator, 'check', { on: Boolean(on), hitTest: !force })
      await settle(p)
      return true
    }),
    choose: withPage(async (p, trigger, option) => {
      await codeClick(p, trigger, { settleAfter: false })
      const roles = ['option', 'menuitem', 'menuitemradio', 'menuitemcheckbox']
      const start = Date.now()
      let target = null
      while (!target && Date.now() - start < 5_000) {
        for (const role of roles) {
          const candidate = locate(p, await resolveTarget(p, { role, name: option }))
          if (await candidate.count()) {
            target = candidate
            break
          }
        }
        if (!target) {
          const inList = p.locator('[role=menu], [role=listbox]').filter({ visible: true }).getByText(option, { exact: false }).filter({ visible: true }).first()
          if (await inList.count()) target = inList
        }
        if (!target && (await locate(p, option).count())) target = locate(p, option)
        if (!target) await p.waitForTimeout(80)
      }
      if (!target) {
        const open = await p.evaluate(() => [...document.querySelectorAll('[role=option], [role=menuitem], [role=menuitemradio], [role=menuitemcheckbox], [role=menu] button, [role=listbox] li, [role=listbox] button')]
          .map((el) => (el.getAttribute('aria-label') || el.innerText || '').replace(/\s+/g, ' ').trim()).filter(Boolean).slice(0, 30))
        throw new Error(`after opening ${JSON.stringify(trigger)} no option or menu item matches ${JSON.stringify(option)}; the open list has: ${open.length ? open.map((name) => JSON.stringify(name)).join(', ') : '(no role=option/menuitem elements - the trigger may not have opened anything)'}`)
      }
      await target.evaluate((el) => {
        const base = { bubbles: true, cancelable: true, composed: true, button: 0, view: window }
        el.dispatchEvent(new PointerEvent('pointerdown', { ...base, pointerId: 1, pointerType: 'mouse', isPrimary: true }))
        el.dispatchEvent(new PointerEvent('pointerup', { ...base, pointerId: 1, pointerType: 'mouse', isPrimary: true }))
        el.click()
      })
      await settle(p)
      return option
    }),
    waitFor: withPage(async (p, target, { state = 'visible', timeout = 15_000 } = {}) => {
      if (typeof target === 'function') {
        await p.waitForFunction(target, undefined, { timeout, polling: 50 })
        return true
      }
      await locate(p, target, { visibleOnly: state === 'visible' }).waitFor({ state, timeout })
      return true
    }),
    waitGone: withPage(async (p, target, { timeout = 15_000 } = {}) => {
      await locate(p, target, { visibleOnly: false }).waitFor({ state: 'hidden', timeout })
      return true
    }),
    waitUrl: withPage(async (p, pattern, { timeout = 15_000 } = {}) => {
      const test = pattern instanceof RegExp ? (url) => pattern.test(url.href) : (url) => url.href.includes(pattern)
      await p.waitForURL(test, { timeout })
      await settle(p)
      return p.url()
    }),
    settle: withPage((p, options) => settle(p, options)),
    text: withPage(async (p, target) => {
      const locator = target ? locate(p, target) : p.locator('body')
      return (await locator.innerText({ timeout: 5_000 })).slice(0, 20_000)
    }),
    value: withPage(async (p, target) => locate(p, target).inputValue({ timeout: 5_000 })),
    attr: withPage(async (p, target, name) => locate(p, target).getAttribute(name, { timeout: 5_000 })),
    count: withPage(async (p, target) => locate(p, target, { visibleOnly: false }).count().then(async (n) => (n ? locate(p, target).count() : 0))),
    exists: withPage(async (p, target) => (await locate(p, target, { visibleOnly: false }).count()) > 0),
    visible: withPage(async (p, target) => locate(p, target).isVisible()),
    tree: withPage(async (p, target, { maxLines } = {}) => ariaTree(p, target, maxLines)),
    evaluate: withPage(async (p, fn, arg) => p.evaluate(fn, arg)),
    url: withPage(async (p) => p.url()),
    annotate: annotationHelpers(withPage),
    shot: async (name, { viewport, focus, full, element, caption, tags } = {}) => {
      if (viewport !== undefined && !['desktop', 'mobile', 'both'].includes(viewport)) {
        throw new Error(`shot viewport must be 'desktop', 'mobile' or 'both' (got ${JSON.stringify(viewport)}); sizes come from the viewports config`)
      }
      const result = await handlers.shot({
        name,
        viewport: viewport === 'both' ? undefined : viewport ?? undefined,
        here: true,
        focus,
        fullPage: Boolean(full),
        selector: element,
        sid: session.sid,
        caption,
        tags,
      })
      return shotLinks(result)
    },
    dump: async (what, name, { viewport, caption, tags } = {}) => {
      const result = await handlers.dump({ what, name, viewport, sid: session.sid, caption, tags })
      const { kept, uploadError, ...links } = result.store ?? {}
      return kept === null ? links : { file: result.file, ...(uploadError ? { uploadError } : {}) }
    },
    har: {
      grouped: true,
      start: async () => handlers.harStart(),
      stop: async ({ session: whole = false, name, caption, tags } = {}) => {
        const result = await handlers.harStop({ name, whole, sid: session.sid, caption, tags })
        return { file: result.file, entries: result.entries, store: result.store }
      },
    },
    consoleExcerpt: async ({ since, name, caption, tags } = {}) => {
      const result = await handlers.consoleExcerpt({ since, name, sid: session.sid, caption, tags })
      return { file: result.file, lines: result.lines, store: result.store }
    },
    video: {
      grouped: true,
      start: async (name, { viewport, focus, pad, pace, fromHere, noPointer, hide, loggedOut, hold = 1000 } = {}) => {
        const result = await handlers.videoStart({ name, viewport, focus, pad, pace, fromHere, noPointer, hide, loggedOut })
        await new Promise((resolve) => setTimeout(resolve, hold))
        return result
      },
      stop: async ({ trim, hold = 1000, caption, tags, motion } = {}) => {
        await settle(activePage(), { timeout: 4_000 })
        await new Promise((resolve) => setTimeout(resolve, hold))
        const result = await handlers.videoStop({ trim, motion, sid: session.sid, caption, tags })
        return { file: result.file, trimmed: result.trimmed, durationSeconds: result.durationSeconds, warnings: result.warnings, annotations: result.annotations, failedInteractions: result.failedInteractions, store: result.store }
      },
    },
    api: async (method, apiPath, body, { headers = {} } = {}) => {
      if (!API_BASE_URL) throw new Error('api() needs apiBaseUrl in the web driver config')
      const extra = {}
      for (const ext of EXTENSIONS) if (ext.apiHeaders) Object.assign(extra, await ext.apiHeaders())
      const response = await fetch(`${API_BASE_URL}${apiPath.startsWith('/') ? '' : '/'}${apiPath}`, {
        method: method.toUpperCase(),
        headers: { 'Content-Type': 'application/json', 'User-Agent': 'eks-harness-web-driver/1.0', ...extra, ...headers },
        body: body === undefined ? undefined : JSON.stringify(body),
      })
      const text = await response.text()
      let data = text
      try {
        data = JSON.parse(text)
      } catch {}
      if (!response.ok) throw new Error(`${method} ${apiPath} -> HTTP ${response.status}: ${text.slice(0, 500)}`)
      return data
    },
    logs: async (kind = 'console', limit = 50) => (await handlers.logs({ kind, limit })).entries,
    clearLogs: async () => handlers.clearLogs(),
    emit: async (name, data) => emitEvent(name, data),
    ...extensionHelpers(),
  }
}


const KIT = {
  config: CONFIG,
  get session() { return session },
  log,
  settle: (page, options) => settle(page ?? activePage(), options),
  gotoPath,
  activePage,
  ensureBrowser,
  ensureMobile,
  syncContexts,
  readPageStorage,
  writePageStorage,
  persist(id, value) {
    session.extensionState[id] = value
    persistSession()
  },
  restore(id) {
    return session.extensionState[id] ?? null
  },
  locate,
  present,
  resolveTarget,
  emit: (name, data, source) => emitEvent(name, data, source),
  daemon: daemonRequest,
  upload: (options) => storeUpload({ sid: session.sid, ...options }),
  helpers: () => scriptHelpers(),
  handlers: () => handlers,
}

const EXTENSIONS = await loadExtensions(CONFIG.extensions ?? [], KIT, log)
const EXTENSION_ROUTES = {}
for (const ext of EXTENSIONS) {
  for (const [route, fn] of Object.entries(ext.routes ?? {})) {
    const key = route.startsWith('/') ? route : `/${route}`
    EXTENSION_ROUTES[key] = async (body) => fn(body ?? {})
  }
}

function extensionHelpers() {
  const merged = {}
  for (const ext of EXTENSIONS) {
    const helpers = typeof ext.helpers === 'function' ? ext.helpers() : ext.helpers
    Object.assign(merged, helpers ?? {})
  }
  return merged
}

const BOXED_STEPS = new Set(['click', 'dblclick', 'fill', 'type', 'hover', 'check', 'select', 'choose', 'wheel', 'waitFor'])
const runner = createRunner({
  helpers: scriptHelpers,
  snapshot: (info) => pageSnapshot(info, runSince),
  onStep: (entry) => {
    const recording = session.recording
    if (!recording) return
    const actionAt = recording.actionAt ?? (Date.now() - (entry.ms ?? 0))
    const actionBox = recording.actionBox
    recording.actionAt = null
    recording.actionBox = null
    const scale = VIEWPORTS[recording.which]?.deviceScaleFactor ?? 1
    const item = {
      at: Number(((actionAt - recording.startedAt) / 1000).toFixed(2)),
      action: entry.name,
      target: scriptTarget(entry.args),
      page: recording.which,
      ok: entry.ok,
      error: entry.error?.slice(0, 300),
      ms: entry.ms,
      ...(actionBox ? { boxes: [{ x: actionBox.x, y: actionBox.y, w: actionBox.width, h: actionBox.height, scale }] } : {}),
    }
    recording.interactions.push(item)
    if (actionBox || !BOXED_STEPS.has(entry.name)) return
    ;(recording.stepEnrich ??= []).push((async () => {
      try {
        const first = entry.args?.[0]
        if (first === undefined || (typeof first !== 'string' && (typeof first !== 'object' || first === null))) return
        const box = await locate(recording.page, first).boundingBox({ timeout: 400 }).catch(() => null)
        if (box) item.boxes = [{ x: box.x, y: box.y, w: box.width, h: box.height, scale }]
      } catch {}
    })())
  },
  log,
})

const RUN_ROUTES = {
  '/run': async (body) => {
    runSince = Date.now()
    return runner.start(String(body.script ?? ''), {
      breakBefore: body.breakBefore ?? [],
      pauseOnError: body.pauseOnError,
      snapshot: body.snapshot,
      shotAtEnd: body.shotAtEnd,
      waitMs: body.waitMs,
      force: body.force,
    })
  },
  '/continue': async (body) => runner.resume(body.action ?? 'continue', { waitMs: body.waitMs, breakBefore: body.breakBefore }),
  '/run-wait': async (body) => runner.wait(body.waitMs),
  '/abort': async () => runner.abort(),
  '/run-status': async (body) => runner.status({ full: Boolean(body.full) }),
  '/exec': async (body) => runner.exec(String(body.script ?? '')),
  '/tree': async (body) => {
    const page = activePage()
    return { ok: true, raw: `${page.url()}\n${await ariaTree(page, body.target || undefined, body.maxLines ?? TREE_MAX_LINES)}`, elapsedMs: 0, steps: [], logs: [] }
  },
}

const server = http.createServer((req, res) => {
  const url = new URL(req.url, `http://127.0.0.1:${PORT}`)
  if (req.method === 'GET' && url.pathname === '/events') return streamEvents(req, res)
  const handlerName = ROUTES[url.pathname]

  const send = (status, body) => {
    if (res.headersSent || res.writableEnded) return
    try {
      const json = JSON.stringify(body, null, 2)
      res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8' })
      res.end(json)
    } catch (error) {
      log(`could not send response: ${error.message}`)
      res.destroy()
    }
  }

  const runRoute = RUN_ROUTES[url.pathname] ?? EXTENSION_ROUTES[url.pathname]
  if (runRoute) {
    let raw = ''
    req.on('data', (chunk) => {
      raw += chunk
    })
    req.on('end', () => {
      let body = {}
      try {
        body = raw ? JSON.parse(raw) : {}
      } catch {
        return send(400, { error: 'body is not valid JSON' })
      }
      const asText = url.searchParams.get('format') === 'text'
      Promise.resolve()
        .then(() => ensureBrowser())
        .then(() => runRoute(body))
        .then((result) => {
          if (!asText) return send(200, result)
          res.writeHead(200, { 'Content-Type': 'text/plain; charset=utf-8' })
          res.end(renderText(result))
        })
        .catch((error) => {
          log(`${url.pathname} failed: ${error?.stack ?? error}`)
          if (!asText) return send(500, { error: error?.message ?? String(error) })
          res.writeHead(500, { 'Content-Type': 'text/plain; charset=utf-8' })
          res.end(`error: ${error?.message ?? String(error)}\n`)
        })
    })
    return
  }

  if (!handlerName) return send(404, { error: `unknown route ${url.pathname}`, routes: [...Object.keys(ROUTES), ...Object.keys(RUN_ROUTES), ...Object.keys(EXTENSION_ROUTES)] })

  let raw = ''
  req.on('data', (chunk) => {
    raw += chunk
  })
  req.on('error', (error) => log(`request error on ${url.pathname}: ${error.message}`))
  req.on('end', () => {
    let body = {}
    if (raw) {
      try {
        body = JSON.parse(raw)
      } catch {
        return send(400, { error: 'body is not valid JSON' })
      }
    }
    for (const [key, value] of url.searchParams) body[key] = value

    const started = Date.now()
    const recording = TRACKED.has(handlerName) ? session.recording : null
    const paced = recording && PACED_ACTIONS.has(handlerName) ? recording : null
    let urlBefore = null
    let actionAt = null
    let actionBox = null
    const track = async (ok, result, error) => {
      if (!recording || session.recording !== recording) return
      const page = activePage()
      const matchCount = result?.matchCount ?? (LOCATOR_ACTIONS.has(handlerName)
        ? await locatorCount(page, body).catch(() => null) : undefined)
      const box = actionBox ?? (LOCATOR_ACTIONS.has(handlerName)
        ? await resolveLocator(page, body).boundingBox().catch(() => null) : null)
      recording.interactions.push({
        at: Number((((actionAt ?? started) - recording.startedAt) / 1000).toFixed(2)),
        action: handlerName,
        target: describeTarget(body),
        page: recording.which,
        url: page?.isClosed() ? null : page?.url(),
        matchCount,
        ok,
        error: error ? String(error?.message ?? error).slice(0, 300) : undefined,
        ms: Date.now() - started,
        ...(box ? {
          boxes: [{
            x: box.x, y: box.y, w: box.width, h: box.height,
            scale: VIEWPORTS[recording.which]?.deviceScaleFactor ?? 1,
          }],
        } : {}),
      })
    }
    Promise.resolve()
      .then(async () => {
        await ensureBrowser()
        try { urlBefore = activePage()?.url() } catch {}
        if (paced && session.recording === recording) {
          actionBox = await presentationLead(activePage(), body, recording.pace, handlerName)
        }
        if (recording) actionAt = Date.now()
      })
      .then(() => (paced ? pressed(activePage(), () => handlers[handlerName](body)) : handlers[handlerName](body)))
      .then(async (result) => {
        if (paced && session.recording === recording) {
          await presentationHold(activePage(), recording.pace, urlBefore)
        }
        await track(true, result)
        log(`${url.pathname} ok ${Date.now() - started}ms`)
        send(200, result)
      })
      .catch(async (error) => {
        await track(false, null, error).catch(() => {})
        log(`${url.pathname} failed after ${Date.now() - started}ms: ${error?.stack ?? error}`)
        send(500, { error: error?.message ?? String(error) })
      })
  })
})

server.on('clientError', (error, socket) => {
  log(`client error: ${error.message}`)
  socket.destroy()
})

const eventClients = new Set()
const eventLog = []

function emitEvent(name, data, source = 'web') {
  const event = { at: new Date().toISOString(), t: Date.now() / 1000, source, name, data: data ?? null }
  eventLog.push(event)
  if (eventLog.length > 1000) eventLog.shift()
  const line = `data: ${JSON.stringify(event)}\n\n`
  for (const res of eventClients) res.write(line)
  return event
}

function streamEvents(req, res) {
  res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', Connection: 'keep-alive' })
  res.write(': connected\n\n')
  eventClients.add(res)
  req.on('close', () => eventClients.delete(res))
}

let heartbeatTimer = null
function startHeartbeat() {
  if (!session.sid) return
  const beat = () => heartbeatSids([session.sid]).then(() => {
    if (leaseReleased(session.sid)) log(`lease ${session.sid} is no longer held`)
  }).catch(() => {})
  beat()
  heartbeatTimer = setInterval(beat, Number(opt('heartbeatMs', 20_000)))
  heartbeatTimer.unref()
}

server.listen(PORT, '127.0.0.1', () => {
  const bound = server.address().port
  const ready = { ready: true, port: bound, pid: process.pid, baseUrl: WEB_BASE_URL, sid: session.sid, extensions: EXTENSIONS.map((ext) => ext.id) }
  fs.writeFileSync(path.join(STATE_DIR, 'worker.json'), JSON.stringify(ready))
  process.stdout.write(`${JSON.stringify(ready)}\n`)
  log(`web driver listening on http://127.0.0.1:${bound} (web ${WEB_BASE_URL})`)
  startHeartbeat()
})

