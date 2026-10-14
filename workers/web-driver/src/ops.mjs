import crypto from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'

import { cssStep, domBoxMapSource, pngSize, recordingDocument, scaledBoxMap } from '../../kit/src/boxmap.mjs'
import { ICONS } from '../../kit/src/icons.mjs'
import { OpError } from '../../kit/src/ops.mjs'
import { describeTarget, strictPick, toWebSpec } from '../../kit/src/targets.mjs'

const DEFAULT_TIMEOUT = 10_000
const POLL_MS = 60
const LONG_PRESS_MS = 800
const SWIPE_FRACTION = 0.6
const MARKED = new Set(['ui.tap', 'ui.double_tap', 'ui.long_press', 'ui.fill', 'ui.type', 'ui.check'])
const OPTION_ROLES = ['option', 'menuitem', 'menuitemradio', 'menuitemcheckbox']
const PACE_FIELDS = [['move', 'moveMs'], ['dwell', 'dwellMs'], ['type_per_char', 'typeMsPerChar'], ['hold', 'holdMs'],
  ['screen_hold', 'screenHoldMs'], ['press', 'mobilePressMs']]
const LOG_KINDS = { console: 'console', errors: 'errors', network: 'network', websocket: 'ws' }
const DEFAULT_ALERTS = '[role=alert]'
const OVERLAY_FADE_MS = 320

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

const LAYOUT_SCRIPT = `(() => {
  const vw = innerWidth, doc = document.documentElement;
  const spills = [];
  const root = document.querySelector('main') || document.body;
  for (const el of root.querySelectorAll('*')) {
    if (el.closest('[data-ehx-overlay]') || el.closest('[data-ehx-ignore]')) continue;
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    if (getComputedStyle(el).position === 'fixed') continue;
    if (r.right > vw + 2) {
      spills.push({ element: el.tagName.toLowerCase() + (el.id ? '#' + el.id : ''), text: (el.innerText || '').trim().slice(0, 40), by: Math.round(r.right - vw) });
      if (spills.length >= 6) break;
    }
  }
  const broken = [...document.images].filter((img) => img.complete && img.naturalWidth === 0 && img.getClientRects().length)
    .slice(0, 6).map((img) => img.currentSrc || img.src);
  return { scrollWidth: doc.scrollWidth, viewportWidth: vw, spills, broken };
})()`

function deadlineFor(args, ctx, { fallback = DEFAULT_TIMEOUT, sessionDefault = true } = {}) {
  const wanted = args?.timeout ?? (sessionDefault ? ctx.session?.timeoutMs : null) ?? fallback
  const left = ctx.remaining()
  const budget = left === null ? wanted : Math.max(0, Math.min(wanted, left - 250))
  return budget
}

function noisy(text, noise) {
  const value = String(text ?? '')
  return noise.some((needle) => needle && value.includes(needle))
}

function setPath(root, dotted, value) {
  const parts = String(dotted).match(/[^.[\]]+/g) ?? []
  if (!parts.length) return value
  let node = root
  for (let index = 0; index < parts.length - 1; index++) {
    const key = /^\d+$/.test(parts[index]) ? Number(parts[index]) : parts[index]
    if (node[key] === null || typeof node[key] !== 'object') node[key] = /^\d+$/.test(parts[index + 1]) ? [] : {}
    node = node[key]
  }
  const last = parts[parts.length - 1]
  node[/^\d+$/.test(last) ? Number(last) : last] = value
  return root
}

function readPath(root, dotted) {
  if (!dotted) return root
  let node = root
  for (const part of String(dotted).match(/[^.[\]]+/g) ?? []) {
    if (node === null || node === undefined) return null
    node = node[/^\d+$/.test(part) ? Number(part) : part]
  }
  return node === undefined ? null : node
}

function globToRegExp(glob) {
  let source = ''
  for (let index = 0; index < glob.length; index++) {
    const char = glob[index]
    if (char === '*') {
      source += '.*'
      while (glob[index + 1] === '*') index++
    } else if (char === '?') {
      source += '.'
    } else {
      source += char.replace(/[.+^${}()|[\]\\]/g, '\\$&')
    }
  }
  return new RegExp(`^${source}$`)
}

export function urlMatcher(pattern) {
  const regex = globToRegExp(String(pattern))
  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(pattern)) return (url) => regex.test(url.href)
  if (String(pattern).startsWith('/')) return (url) => regex.test(url.pathname + url.search) || regex.test(url.pathname)
  return (url) => regex.test(url.href) || globToRegExp(`*${pattern}*`).test(url.href)
}

function camelPace(pace) {
  if (!pace) return undefined
  const out = {}
  for (const [snake, camel] of PACE_FIELDS) if (pace[snake] !== undefined && pace[snake] !== null) out[camel] = pace[snake]
  return out
}

function textOutcome(kind, actual, { equals, contains, matches }) {
  const value = actual === null || actual === undefined ? null : String(actual)
  if (equals !== null && equals !== undefined && value !== equals) return `${kind} is ${JSON.stringify(value)}, expected ${JSON.stringify(equals)}`
  if (contains !== null && contains !== undefined && !(value ?? '').includes(contains)) return `${kind} ${JSON.stringify(value)} does not contain ${JSON.stringify(contains)}`
  if (matches !== null && matches !== undefined && !new RegExp(matches).test(value ?? '')) return `${kind} ${JSON.stringify(value)} does not match /${matches}/`
  return null
}

function normalized(text) {
  return String(text ?? '').replace(/\s+/g, ' ').trim()
}

export function defineWebOps(define, W) {
  const checkpoints = new Map()
  const harMarks = new Map()
  const routes = []
  let shape = null
  let cassette = null
  let annoCounter = 0
  let boxSteps = null

  const page = () => W.activePage()

  async function writeBoxMap(p, ctx, file, name, { element = null, full = false } = {}) {
    try {
      let opts = { full: Boolean(full) && !element }
      if (element) {
        const box = await element.boundingBox()
        if (!box) return
        opts = { offsetX: -box.x, offsetY: -box.y, width: box.width, height: box.height }
      }
      const walk = await p.evaluate(domBoxMapSource(opts))
      const sidecar = file.replace(/\.png$/, '.boxes.json')
      fs.writeFileSync(sidecar, JSON.stringify(scaledBoxMap(walk, pngSize(fs.readFileSync(file)))))
      ctx.addFile({ path: sidecar, role: 'evidence', kind: 'file', name: path.basename(sidecar), caption: `box map of ${name}` })
    } catch (error) {
      ctx.log?.(`box map of ${name} skipped: ${error.message}`)
    }
  }

  async function snapRecording(action) {
    const rec = W.session.recording
    if (!boxSteps || !rec) return
    const at = (Date.now() - rec.startedAt) / 1000
    const walk = await page().evaluate(domBoxMapSource({})).catch(() => null)
    if (walk) boxSteps.push({ at, action, walk })
  }

  function recordingBoxes(name, video, result, crop) {
    const steps = boxSteps ?? []
    boxSteps = null
    if (!steps.length) return null
    const first = (result.firstFrameMs ?? 0) / 1000
    let events = []
    try {
      const log = JSON.parse(fs.readFileSync(result.interactionsLog, 'utf8'))
      events = (log.interactions ?? []).map((entry) => Math.max(0, Number(entry.at) - first)).filter((value) => Number.isFinite(value))
    } catch {}
    return recordingDocument({
      video: path.basename(video),
      units: 'css',
      steps: steps.map((step) => cssStep(step.walk, Math.max(0, step.at - first), step.action)),
      events,
      crop: crop ? { x: crop.x, y: crop.y, width: crop.width, height: crop.height } : null,
      trimmed: Boolean(result.trimmed),
      duration: result.durationSeconds,
    })
  }

  W.onContext(async (context) => {
    for (const entry of routes) await context.route(entry.matcher, entry.handler)
  })

  async function candidates(p, spec, visibleOnly = true) {
    const { nth, near, point, ...base } = spec
    return W.locate(p, base, { all: true, visibleOnly })
  }

  async function labels(locator) {
    return locator.evaluateAll((els) => els.slice(0, 10).map((el) => {
      const text = (el.getAttribute('aria-label') || el.innerText || el.getAttribute('placeholder') || el.value || '').replace(/\s+/g, ' ').trim().slice(0, 80)
      const id = el.getAttribute('data-testid')
      return `${el.tagName.toLowerCase()}${id ? `[data-testid=${id}]` : ''}${text ? ` "${text}"` : ''}`
    })).catch(() => [])
  }

  async function nearMisses(p, spec) {
    const text = spec.text ?? spec.name ?? spec.label ?? spec.placeholder
    return text ? W.nearMisses(p, String(text)) : []
  }

  async function waitCount(locator, timeout) {
    const deadline = Date.now() + timeout
    for (;;) {
      const count = await locator.count().catch(() => 0)
      if (count > 0 || Date.now() >= deadline) return count
      await sleep(POLL_MS)
    }
  }

  async function centreOf(locator) {
    const box = await locator.boundingBox().catch(() => null)
    return box ? { x: box.x + box.width / 2, y: box.y + box.height / 2 } : null
  }

  async function resolve(p, target, { timeout, visibleOnly = true } = {}) {
    const spec = toWebSpec(target)
    if (spec.point) return { point: spec.point, spec, target }
    const all = await candidates(p, spec, visibleOnly)
    const count = await waitCount(all, timeout)
    if (spec.near) {
      if (count === 0) strictPick(0, target, await nearMisses(p, spec))
      const anchor = await resolve(p, target.near, { timeout: 1_000 })
      const from = anchor.point ?? (await centreOf(anchor.locator))
      if (!from) throw new OpError('target_not_found', `the near anchor ${describeTarget(target.near)} has no box`, { detail: { target }, retryable: true })
      const boxes = await all.evaluateAll((els) => els.map((el) => {
        const r = el.getBoundingClientRect()
        return { x: r.left + r.width / 2, y: r.top + r.height / 2 }
      }))
      const order = boxes.map((box, index) => ({ index, distance: Math.hypot(box.x - from.x, box.y - from.y) }))
        .sort((a, b) => a.distance - b.distance)
      const nth = spec.nth ?? 0
      const picked = order[nth < 0 ? order.length + nth : nth]
      if (!picked) strictPick(count, { ...target, nth }, [])
      return { locator: all.nth(picked.index), spec, target, count }
    }
    const index = strictPick(count, target, count ? await labels(all) : await nearMisses(p, spec))
    return { locator: all.nth(index), spec, target, count }
  }

  function interaction(name, target, started, ok, error, box) {
    W.recordInteraction({ action: name, target: target ? describeTarget(target) : null, started, ok, error, box })
  }

  async function act(ctx, args, target, run, { settle = args.settle !== false } = {}) {
    const p = page()
    const timeout = deadlineFor(args, ctx)
    const found = target ? await resolve(p, target, { timeout }) : null
    const before = p.url()
    const pace = W.recPace()
    let box = null
    if (pace && found?.locator) box = await W.leadTo(p, found.locator, pace, { mark: MARKED.has(ctx.op) })
    W.stampAction(box)
    const started = Date.now()
    try {
      const value = await W.pressed(p, () => run(p, found, timeout))
      interaction(ctx.op, target, started, true, null, box)
      if (pace) await W.scriptHold(p, before)
      if (settle) await W.settle(page(), { timeout: Math.min(10_000, Math.max(1_000, ctx.remaining() ?? 10_000)) })
      await snapRecording(ctx.op)
      W.persistSession()
      return value
    } catch (error) {
      interaction(ctx.op, target, started, false, error, box)
      throw error
    }
  }

  const realInput = (args) => args.real ?? Boolean(W.config.realInput)

  async function click(p, found, args, timeout, count = 1) {
    if (found.point) {
      await p.mouse.click(found.point.x, found.point.y, { clickCount: count })
      return
    }
    if (realInput(args)) {
      if (count === 2) await found.locator.dblclick({ timeout, force: Boolean(args.force) })
      else await found.locator.click({ timeout, force: Boolean(args.force) })
      return
    }
    await W.releaseHover(p)
    await W.inPage(p, found.locator, 'click', { hitTest: !args.force, count }, { timeout })
  }

  async function fillValue(p, found, args, value, timeout) {
    if (found.point) throw new OpError('target_unsupported', 'fill needs an element target, not a point', { detail: { target: found.target } })
    if (realInput(args)) await found.locator.fill(value, { timeout })
    else await W.inPage(p, found.locator, 'fill', { value }, { timeout })
  }

  async function submit(p, found, args) {
    if (realInput(args)) await found.locator.press('Enter')
    else await W.pressInPage(p, 'Enter', found.locator)
  }

  async function pointOf(found) {
    if (found.point) return found.point
    await found.locator.scrollIntoViewIfNeeded({ timeout: 5_000 }).catch(() => {})
    const centre = await centreOf(found.locator)
    if (!centre) throw new OpError('target_not_found', `${describeTarget(found.target)} has no box on screen`, { retryable: true })
    return centre
  }

  async function scrollBy(p, found, dx, dy, args, timeout) {
    if (found?.locator && !realInput(args)) return W.wheelInPage(p, found.locator, dx, dy, timeout)
    if (found) {
      const at = await pointOf(found)
      await p.mouse.move(at.x, at.y)
    }
    if (realInput(args) || found) await p.mouse.wheel(dx, dy)
    else await p.evaluate(([x, y]) => (document.scrollingElement || document.documentElement).scrollBy({ left: x, top: y, behavior: 'instant' }), [dx, dy])
    return null
  }

  async function withViewport(p, viewport, run) {
    if (!viewport) return run()
    const current = p.viewportSize()
    const same = current && current.width === viewport.width && current.height === viewport.height && !viewport.mobile
    if (same) return run()
    const cdp = await p.context().newCDPSession(p)
    try {
      await cdp.send('Emulation.setDeviceMetricsOverride', {
        width: viewport.width, height: viewport.height, deviceScaleFactor: viewport.scale ?? 1, mobile: Boolean(viewport.mobile),
      })
      if (viewport.touch) await cdp.send('Emulation.setTouchEmulationEnabled', { enabled: true, maxTouchPoints: 5 })
      await p.evaluate(() => window.dispatchEvent(new Event('resize'))).catch(() => {})
      await W.settle(p, { timeout: 5_000 })
      return await run()
    } finally {
      await cdp.send('Emulation.clearDeviceMetricsOverride').catch(() => {})
      if (viewport.touch) await cdp.send('Emulation.setTouchEmulationEnabled', { enabled: false }).catch(() => {})
      await cdp.detach().catch(() => {})
      await p.evaluate(() => window.dispatchEvent(new Event('resize'))).catch(() => {})
    }
  }

  async function markAll(p, targets, attribute, value, timeout = 3_000) {
    const marked = []
    for (const target of targets ?? []) {
      const spec = toWebSpec(target)
      const all = await candidates(p, spec, false)
      await waitCount(all, timeout)
      const locator = spec.nth !== undefined && spec.nth !== null ? all.nth(spec.nth) : all
      await locator.evaluateAll((els, [name, mark]) => els.forEach((el) => el.setAttribute(name, mark)), [attribute, value]).catch(() => {})
      marked.push(target)
    }
    return async () => {
      await p.evaluate(([name, mark]) => {
        for (const el of document.querySelectorAll(`[${name}="${mark}"]`)) el.removeAttribute(name)
      }, [attribute, value]).catch(() => {})
    }
  }

  async function capturePng(p, file, { element, full }) {
    await W.setPointerVisible(p, false)
    try {
      if (element) await element.screenshot({ path: file })
      else await p.screenshot({ path: file, fullPage: Boolean(full) })
    } finally {
      await W.setPointerVisible(p, true)
    }
  }

  async function shot(ctx, args, { name, role }) {
    const p = page()
    const file = ctx.file(`${name}.png`)
    const token = `h${crypto.randomBytes(4).toString('hex')}`
    await withViewport(p, args.viewport, async () => {
      const unhide = await markAll(p, args.hide, 'data-ehx-hide', token)
      await W.applyHide(p, W.hideSelectorsFor([`[data-ehx-hide="${token}"]`]))
      try {
        if (args.focus) {
          const focus = await resolve(p, args.focus, { timeout: deadlineFor(args, ctx, { fallback: 5_000 }) })
          if (focus.locator) await focus.locator.scrollIntoViewIfNeeded({ timeout: 5_000 }).catch(() => {})
        }
        const element = args.element ? (await resolve(p, args.element, { timeout: deadlineFor(args, ctx, { fallback: 5_000 }) })).locator : null
        await capturePng(p, file, { element, full: args.full })
        await writeBoxMap(p, ctx, file, name, { element, full: args.full })
      } finally {
        await W.clearHide(p)
        await unhide()
      }
    })
    ctx.addFile({ path: file, role, kind: 'screenshot', name })
    return file
  }

  async function chooseOption(p, option, timeout) {
    const deadline = Date.now() + timeout
    while (Date.now() < deadline) {
      for (const role of OPTION_ROLES) {
        const found = W.locate(p, { role, name: option, nameMatch: 'exact' }, { all: true })
        if (await found.count().catch(() => 0)) return found.first()
      }
      const inList = p.locator('[role=menu], [role=listbox]').filter({ visible: true }).getByText(option, { exact: true }).filter({ visible: true })
      if (await inList.count().catch(() => 0)) return inList.first()
      await sleep(80)
    }
    const open = await p.evaluate(() => [...document.querySelectorAll('[role=option], [role=menuitem], [role=menuitemradio], [role=menuitemcheckbox]')]
      .map((el) => (el.getAttribute('aria-label') || el.innerText || '').replace(/\s+/g, ' ').trim()).filter(Boolean).slice(0, 30)).catch(() => [])
    throw new OpError('target_not_found', `no option or menu item is named ${JSON.stringify(option)}; the open list has ${open.length ? open.map((name) => JSON.stringify(name)).join(', ') : 'no options'}`, { detail: { option, options: open }, retryable: true })
  }

  for (const [name, count] of [['ui.tap', 1], ['ui.double_tap', 2]]) {
    define({
      name,
      run: (args, ctx) => act(ctx, args, args.target, (p, found, timeout) => click(p, found, args, timeout, count)),
    })
  }

  define({
    name: 'ui.long_press',
    run: (args, ctx) => act(ctx, args, args.target, async (p, found) => {
      const at = await pointOf(found)
      await p.mouse.move(at.x, at.y)
      await p.mouse.down()
      await sleep(args.hold ?? LONG_PRESS_MS)
      await p.mouse.up()
    }),
  })

  define({
    name: 'ui.fill',
    run: (args, ctx) => act(ctx, args, args.target, async (p, found, timeout) => {
      await fillValue(p, found, args, String(args.value ?? ''), timeout)
      if (args.submit) await submit(p, found, args)
    }),
  })

  define({
    name: 'ui.clear',
    run: (args, ctx) => act(ctx, args, args.target, (p, found, timeout) => fillValue(p, found, args, '', timeout)),
  })

  define({
    name: 'ui.type',
    run: (args, ctx) => act(ctx, args, args.target, async (p, found, timeout) => {
      if (found.point) throw new OpError('target_unsupported', 'type needs an element target, not a point')
      const perChar = args.pace ? W.parsePace(args.pace).typeMsPerChar : W.recPace()?.typeMsPerChar ?? 0
      const value = String(args.value ?? '')
      if (realInput(args)) {
        await found.locator.pressSequentially(value, { delay: perChar, timeout: Math.max(timeout, value.length * (perChar + 50)) })
      } else {
        for (const character of Array.from(value)) {
          await W.inPage(p, found.locator, 'type', { character }, { timeout })
          if (perChar) await sleep(perChar)
        }
      }
      if (args.submit) await submit(p, found, args)
    }),
  })

  define({
    name: 'ui.hover',
    run: (args, ctx) => act(ctx, args, args.target, async (p, found, timeout) => {
      if (found.point) return p.mouse.move(found.point.x, found.point.y)
      if (realInput(args)) return found.locator.hover({ timeout })
      await W.hoverInPage(p, found.locator, timeout)
    }),
  })

  define({
    name: 'ui.key',
    run: (args, ctx) => act(ctx, args, args.target ?? null, async (p, found) => {
      const locator = found?.locator ?? null
      if (realInput(args)) {
        if (locator) await locator.press(args.key)
        else await p.keyboard.press(args.key)
      } else {
        await W.pressInPage(p, args.key, locator)
      }
    }),
  })

  define({
    name: 'ui.select',
    run: (args, ctx) => act(ctx, args, args.target, async (p, found, timeout) => {
      if (found.point) throw new OpError('target_unsupported', 'select needs a <select> element target')
      const values = Array.isArray(args.value) ? args.value : [args.value]
      const chosen = await found.locator.selectOption(values.map((value) => ({ label: value })), { timeout }).catch(() => null)
      if (chosen && chosen.length) return
      await found.locator.selectOption(values, { timeout })
    }),
  })

  define({
    name: 'ui.check',
    run: (args, ctx) => act(ctx, args, args.target, async (p, found, timeout) => {
      if (found.point) throw new OpError('target_unsupported', 'check needs an element target')
      const on = args.on !== false
      if (realInput(args)) await found.locator.setChecked(on, { timeout })
      else await W.inPage(p, found.locator, 'check', { on, hitTest: true }, { timeout })
    }),
  })

  define({
    name: 'ui.choose',
    run: (args, ctx) => act(ctx, args, args.trigger, async (p, found, timeout) => {
      await click(p, found, args, timeout)
      const option = await chooseOption(p, args.option, timeout)
      if (realInput(args)) await option.click({ timeout })
      else await W.inPage(p, option, 'click', { hitTest: false }, { timeout })
    }),
  })

  define({
    name: 'ui.drag',
    run: (args, ctx) => act(ctx, args, args.source, async (p, found, timeout) => {
      const to = await resolve(p, args.to, { timeout })
      const from = await pointOf(found)
      const end = await pointOf(to)
      await p.mouse.move(from.x, from.y)
      await p.mouse.down()
      const steps = 12
      for (let step = 1; step <= steps; step++) {
        await p.mouse.move(from.x + ((end.x - from.x) * step) / steps, from.y + ((end.y - from.y) * step) / steps)
      }
      await p.mouse.up()
    }),
  })

  define({
    name: 'ui.scroll',
    run: (args, ctx) => act(ctx, args, args.target ?? null, async (p, found, timeout) => {
      const to = args.to
      if (to && typeof to === 'object') {
        const item = await resolve(p, to, { timeout })
        if (item.locator) await item.locator.scrollIntoViewIfNeeded({ timeout })
        return
      }
      if (typeof to === 'string') {
        const box = found?.locator ?? p.locator(':root')
        await box.evaluate((el, edge) => {
          const scroller = el === document.documentElement ? (document.scrollingElement || el) : el
          const options = { behavior: 'instant' }
          if (edge === 'top') options.top = 0
          if (edge === 'bottom') options.top = scroller.scrollHeight
          if (edge === 'start') options.left = 0
          if (edge === 'end') options.left = scroller.scrollWidth
          scroller.scrollTo(options)
        }, to)
        return
      }
      const [dx, dy] = Array.isArray(args.by) ? args.by.map(Number) : [0, 400]
      await scrollBy(p, found, dx, dy, args, timeout)
    }),
  })

  define({
    name: 'ui.swipe',
    run: (args, ctx) => act(ctx, args, args.target ?? null, async (p, found, timeout) => {
      const size = found?.locator ? await found.locator.boundingBox() : p.viewportSize()
      const fraction = args.distance ?? SWIPE_FRACTION
      const dx = { left: 1, right: -1 }[args.direction] ?? 0
      const dy = { up: 1, down: -1 }[args.direction] ?? 0
      await scrollBy(p, found, Math.round(dx * fraction * (size?.width ?? 1280)), Math.round(dy * fraction * (size?.height ?? 720)), args, timeout)
    }),
  })

  define({
    name: 'web.wheel',
    run: (args, ctx) => act(ctx, args, args.target ?? null, (p, found, timeout) => scrollBy(p, found, args.dx ?? 0, args.dy ?? 400, args, timeout), { settle: true }),
  })

  async function navigated(ctx, run) {
    const started = Date.now()
    try {
      const url = await run()
      interaction(ctx.op, null, started, true)
      return url
    } catch (error) {
      interaction(ctx.op, null, started, false, error)
      if (error instanceof OpError) throw error
      throw new OpError('navigation', `${ctx.op}: ${error?.message ?? error}`, { retryable: true })
    }
  }

  define({
    name: 'ui.nav',
    run: (args, ctx) => navigated(ctx, async () => {
      let href = String(args.route)
      if (args.params && Object.keys(args.params).length) {
        const url = new URL(href, W.baseUrl)
        for (const [key, value] of Object.entries(args.params)) url.searchParams.set(key, typeof value === 'string' ? value : JSON.stringify(value))
        href = url.pathname + url.search + url.hash
      }
      await W.nav(href)
      return null
    }),
  })

  define({ name: 'ui.open_url', run: (args, ctx) => navigated(ctx, async () => { await W.gotoPath(String(args.url)); return null }) })
  define({ name: 'ui.reload', run: (args, ctx) => navigated(ctx, async () => { await W.reload(); return null }) })
  define({ name: 'ui.back', run: (args, ctx) => navigated(ctx, async () => { await W.back(); return null }) })

  define({
    name: 'ui.settle',
    run: async (args, ctx) => {
      const outcome = await W.settle(page(), { timeout: deadlineFor(args, ctx) })
      if (outcome.timedOut && args.strict !== false) {
        throw new OpError('timeout', `the page did not settle within ${outcome.ms} ms (network or DOM kept changing)`, { detail: outcome, retryable: true })
      }
      return null
    },
  })

  async function waitState(p, target, state, timeout) {
    const spec = toWebSpec(target)
    const deadline = Date.now() + timeout
    for (;;) {
      const visible = await candidates(p, spec, true)
      const attached = await candidates(p, spec, false)
      const shown = await visible.count().catch(() => 0)
      const present = await attached.count().catch(() => 0)
      let done = false
      if (state === 'hidden') done = shown === 0
      else if (state === 'attached') done = present > 0
      else if (state === 'visible') done = shown > 0
      else if (state === 'enabled') done = shown > 0 && (await visible.nth(strictPick(shown, target, await labels(visible))).isEnabled().catch(() => false))
      if (done) {
        if (state === 'visible' && shown > 1) strictPick(shown, target, await labels(visible))
        if (state === 'attached' && present > 1) strictPick(present, target, await labels(attached))
        return
      }
      if (Date.now() >= deadline) {
        const what = state === 'hidden' ? `${describeTarget(target)} is still visible` : `${describeTarget(target)} did not become ${state}`
        throw new OpError(state === 'hidden' || present ? 'timeout' : 'target_not_found', `${what} within ${timeout} ms`, { detail: { target, state, matches: present }, retryable: true })
      }
      await sleep(POLL_MS)
    }
  }

  define({ name: 'ui.wait_for', run: async (args, ctx) => { await waitState(page(), args.target, args.state ?? 'visible', deadlineFor(args, ctx)); return null } })
  define({ name: 'ui.wait_gone', run: async (args, ctx) => { await waitState(page(), args.target, 'hidden', deadlineFor(args, ctx)); return null } })

  define({
    name: 'ui.wait_url',
    run: async (args, ctx) => {
      const p = page()
      const timeout = deadlineFor(args, ctx)
      try {
        await p.waitForURL((url) => url.href.includes(args.pattern), { timeout })
      } catch {
        throw new OpError('timeout', `the URL ${p.url()} did not come to contain ${JSON.stringify(args.pattern)} within ${timeout} ms`, { detail: { url: p.url(), pattern: args.pattern }, retryable: true })
      }
      await W.settle(p, { timeout: 5_000 })
      return p.url()
    },
  })

  const readTimeout = (args, ctx) => deadlineFor(args, ctx, { fallback: 5_000 })

  define({
    name: 'ui.text',
    run: async (args, ctx) => {
      const p = page()
      const found = args.target ? await resolve(p, args.target, { timeout: readTimeout(args, ctx) }) : null
      if (found?.point) throw new OpError('target_unsupported', 'text needs an element target')
      const locator = found?.locator ?? p.locator('body')
      return normalized(await locator.innerText({ timeout: 5_000 })).slice(0, 20_000)
    },
  })

  define({
    name: 'ui.value',
    run: async (args, ctx) => {
      const found = await resolve(page(), args.target, { timeout: readTimeout(args, ctx) })
      if (found.point) throw new OpError('target_unsupported', 'value needs an element target')
      return found.locator.inputValue({ timeout: 5_000 })
    },
  })

  define({
    name: 'ui.attr',
    run: async (args, ctx) => {
      const found = await resolve(page(), args.target, { timeout: readTimeout(args, ctx), visibleOnly: false })
      if (found.point) throw new OpError('target_unsupported', 'attr needs an element target')
      return found.locator.getAttribute(args.name, { timeout: 5_000 })
    },
  })

  define({
    name: 'ui.count',
    run: async (args) => {
      const all = await candidates(page(), toWebSpec(args.target), true)
      return waitCount(all, args.timeout ?? 0)
    },
  })

  define({
    name: 'ui.exists',
    run: async (args) => (await waitCount(await candidates(page(), toWebSpec(args.target), false), args.timeout ?? 0)) > 0,
  })

  define({
    name: 'ui.visible',
    run: async (args) => (await waitCount(await candidates(page(), toWebSpec(args.target), true), args.timeout ?? 0)) > 0,
  })

  define({
    name: 'ui.tree',
    run: async (args, ctx) => {
      const p = page()
      const found = args.target ? await resolve(p, args.target, { timeout: readTimeout(args, ctx) }) : null
      if (found?.point) throw new OpError('target_unsupported', 'tree needs an element target')
      return W.ariaOf(found?.locator ?? p.locator('body'), args.max_lines ?? W.treeMaxLines)
    },
  })

  define({ name: 'ui.url', run: async () => page().url() })

  define({
    name: 'ui.route',
    run: async () => {
      const url = new URL(page().url())
      return { name: url.pathname, params: Object.fromEntries(url.searchParams), path: url.pathname, query: url.search, hash: url.hash, url: url.href }
    },
  })

  define({
    name: 'ui.state',
    run: async (args) => {
      const state = await page().evaluate(async (dotted) => {
        const hook = window.__ehxState
        if (typeof hook !== 'function') return { missing: true }
        return { value: await hook(dotted ?? null) }
      }, args.path ?? null)
      if (state.missing) throw new OpError('op_unsupported', 'the page exposes no state hook: the web app must define window.__ehxState(path) for ui.state', { detail: { op: 'ui.state' } })
      return state.value === undefined ? null : readPath(state.value, null)
    },
  })

  define({
    name: 'ui.layout',
    run: async (args, ctx) => {
      const p = page()
      const found = await resolve(p, args.target, { timeout: readTimeout(args, ctx), visibleOnly: false })
      if (found.point) return { x: found.point.x, y: found.point.y, width: 0, height: 0, visible: true, inViewport: true }
      return found.locator.evaluate((el) => {
        const r = el.getBoundingClientRect()
        const style = getComputedStyle(el)
        const visible = r.width > 0 && r.height > 0 && style.visibility !== 'hidden' && style.display !== 'none'
        return {
          x: r.left, y: r.top, width: r.width, height: r.height, visible,
          inViewport: visible && r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth,
          viewport: { width: innerWidth, height: innerHeight }, scroll: { x: scrollX, y: scrollY },
        }
      })
    },
  })

  async function assertOnce(p, target, assertion) {
    const spec = toWebSpec(target)
    const visible = await candidates(p, spec, true)
    const shown = await visible.count().catch(() => 0)
    const kind = assertion.kind
    if (kind === 'visible') return { failure: shown ? null : `${describeTarget(target)} is not visible`, actual: shown }
    if (kind === 'hidden') return { failure: shown ? `${describeTarget(target)} is visible (${shown} match)` : null, actual: shown }
    if (kind === 'count') {
      const { equals, at_least: atLeast, at_most: atMost } = assertion
      let failure = null
      if (equals !== null && equals !== undefined && shown !== equals) failure = `${shown} elements match ${describeTarget(target)}, expected ${equals}`
      else if (atLeast !== null && atLeast !== undefined && shown < atLeast) failure = `${shown} elements match ${describeTarget(target)}, expected at least ${atLeast}`
      else if (atMost !== null && atMost !== undefined && shown > atMost) failure = `${shown} elements match ${describeTarget(target)}, expected at most ${atMost}`
      return { failure, actual: shown }
    }
    const pool = kind === 'attr' ? await candidates(p, spec, false) : visible
    const count = kind === 'attr' ? await pool.count().catch(() => 0) : shown
    if (count === 0) return { failure: `no element matches ${describeTarget(target)}`, actual: null, missing: true }
    const element = pool.nth(strictPick(count, target, await labels(pool)))
    if (kind === 'enabled' || kind === 'disabled') {
      const enabled = await element.isEnabled()
      return { failure: enabled === (kind === 'enabled') ? null : `${describeTarget(target)} is ${enabled ? 'enabled' : 'disabled'}`, actual: enabled }
    }
    if (kind === 'checked') {
      const checked = await element.isChecked()
      const on = assertion.on !== false
      return { failure: checked === on ? null : `${describeTarget(target)} is ${checked ? 'checked' : 'not checked'}`, actual: checked }
    }
    if (kind === 'text') {
      const actual = normalized(await element.innerText())
      return { failure: textOutcome('text', actual, assertion), actual }
    }
    if (kind === 'value') {
      const actual = await element.inputValue()
      return { failure: textOutcome('value', actual, assertion), actual }
    }
    if (kind === 'attr') {
      const actual = await element.getAttribute(assertion.name)
      return { failure: textOutcome(`attribute ${assertion.name}`, actual, assertion), actual }
    }
    throw new OpError('args_invalid', `unknown assertion ${kind}`)
  }

  define({
    name: 'expect.target',
    run: async (args, ctx) => {
      const p = page()
      const timeout = deadlineFor(args, ctx, { fallback: 5_000 })
      const deadline = Date.now() + timeout
      let last
      for (;;) {
        last = await assertOnce(p, args.target, args.assertion)
        if (!last.failure) return { ok: true, assertion: args.assertion.kind }
        if (args.eventually === false || Date.now() >= deadline) break
        await sleep(POLL_MS * 2)
      }
      const message = args.message ? `${args.message}: ${last.failure}` : last.failure
      throw new OpError('assertion', message, { detail: { assertion: args.assertion, actual: last.actual ?? null, target: args.target, soft: Boolean(args.soft) } })
    },
  })

  const sessionNoise = (ctx, params) => [...(ctx.session?.noise ?? []), ...(params?.noise ?? [])]

  async function runCheck(p, use, since, ctx) {
    const params = use.params ?? {}
    const kind = use.kind
    const fresh = (list) => list.filter((entry) => !entry.at || Date.parse(entry.at) >= since)
    const outcome = (ok, message, detail = {}) => ({ kind, ok, severity: 'error', message, detail })
    if (kind === 'console_errors') {
      const found = fresh(W.session.console).filter((entry) => entry.type === 'error' && !noisy(entry.text, sessionNoise(ctx, params)))
        .map((entry) => String(entry.text).slice(0, 300))
      return outcome(!found.length, found.length ? `${found.length} console error(s): ${found.slice(-3).join(' | ')}` : 'no console errors', { errors: found.slice(-10) })
    }
    if (kind === 'page_errors') {
      const found = fresh(W.session.pageErrors).map((entry) => String(entry.message ?? entry.text ?? '').slice(0, 300))
        .filter((text) => !noisy(text, sessionNoise(ctx, params)))
      return outcome(!found.length, found.length ? `${found.length} page error(s): ${found.slice(-3).join(' | ')}` : 'no page errors', { errors: found.slice(-10) })
    }
    if (kind === 'failed_requests') {
      const allowed = new Set(params.allow_status ?? [])
      const found = fresh(W.session.failedRequests)
        .filter((entry) => W.isAppUrl(entry.url) && !allowed.has(entry.status) && !noisy(entry.url, sessionNoise(ctx, params)))
        .map((entry) => `${entry.status ?? entry.failure ?? 'failed'} ${entry.method ?? ''} ${String(entry.url ?? '').split('?')[0].slice(0, 200)}`.replace(/\s+/g, ' ').trim())
      return outcome(!found.length, found.length ? `${found.length} failed request(s): ${found.slice(-3).join(' | ')}` : 'no failed requests', { requests: found.slice(-10) })
    }
    if (kind === 'layout_spill' || kind === 'horizontal_scroll' || kind === 'broken_images') {
      const token = `i${crypto.randomBytes(4).toString('hex')}`
      const unmark = kind === 'layout_spill' ? await markAll(p, params.ignore, 'data-ehx-ignore', token, 500) : async () => {}
      try {
        const layout = await p.evaluate(LAYOUT_SCRIPT)
        if (kind === 'layout_spill') {
          return outcome(!layout.spills.length, layout.spills.length ? `${layout.spills.length} element(s) spill past the viewport: ${layout.spills.map((s) => `${s.element} "${s.text}" by ${s.by}px`).join(', ')}` : 'nothing spills past the viewport', { spills: layout.spills })
        }
        if (kind === 'horizontal_scroll') {
          const scrolls = layout.scrollWidth > layout.viewportWidth + 1
          return outcome(!scrolls, scrolls ? `the page scrolls horizontally (${layout.scrollWidth}px > ${layout.viewportWidth}px)` : 'no horizontal scroll', { scrollWidth: layout.scrollWidth, viewportWidth: layout.viewportWidth })
        }
        return outcome(!layout.broken.length, layout.broken.length ? `${layout.broken.length} broken image(s): ${layout.broken.join(', ')}` : 'no broken images', { images: layout.broken })
      } finally {
        await unmark()
      }
    }
    if (kind === 'error_toasts') {
      const toasts = params.selector
        ? await (await candidates(p, toWebSpec(params.selector), true)).allInnerTexts().catch(() => [])
        : await p.locator(W.config.alerts || DEFAULT_ALERTS).filter({ visible: true }).allInnerTexts().catch(() => [])
      const texts = toasts.map(normalized).filter(Boolean).slice(0, 5)
      return outcome(!texts.length, texts.length ? `error toast: ${texts.join(' | ')}` : 'no error toasts', { toasts: texts })
    }
    if (kind === 'route') {
      const url = new URL(p.url())
      const expected = String(params.expected ?? '')
      const ok = expected.includes('*') ? globToRegExp(expected).test(url.pathname) : url.pathname === expected
      return outcome(ok, ok ? `on ${url.pathname}` : `on ${url.pathname}, expected ${expected}`, { path: url.pathname })
    }
    return { kind, ok: false, severity: 'error', message: `the check ${kind} is not supported on web by this worker`, detail: { code: 'check_unsupported' } }
  }

  define({
    name: 'check.checkpoint',
    run: async (args, ctx) => {
      const p = page()
      const key = ctx.session?.key ?? 'main'
      const since = checkpoints.get(key) ?? W.startedAt
      const now = Date.now()
      const label = args.label
      const checks = []
      await withViewport(p, args.viewport, async () => {
        for (const use of args.checks ?? []) checks.push(await runCheck(p, use, since, ctx))
        if (args.shot !== false) {
          const file = ctx.file(`${label.replace(/[^A-Za-z0-9._-]+/g, '-') || 'checkpoint'}.png`)
          await capturePng(p, file, { full: args.full })
          ctx.addFile({ path: file, role: 'shot', kind: 'screenshot', name: label, caption: `checkpoint ${label}` })
          await writeBoxMap(p, ctx, file, label, { full: args.full })
        }
      })
      checkpoints.set(key, now)
      return {
        label,
        viewport: args.viewport?.name ?? ctx.session?.viewport ?? null,
        ok: checks.every((check) => check.ok || check.severity !== 'error'),
        checks,
        shots: [],
      }
    },
  })

  define({
    name: 'capture.shot',
    run: async (args, ctx) => {
      await shot(ctx, args, { name: args.name, role: 'value' })
      return null
    },
  })

  define({
    name: 'capture.dump',
    run: async (args, ctx) => {
      const p = page()
      const name = args.name ?? args.kind
      let file
      await withViewport(p, args.viewport, async () => {
        if (args.kind === 'dom') {
          file = ctx.file(`${name}.html`)
          fs.writeFileSync(file, await p.content())
        } else if (args.kind === 'mhtml') {
          const cdp = await p.context().newCDPSession(p)
          try {
            const snapshot = await cdp.send('Page.captureSnapshot', { format: 'mhtml' })
            file = ctx.file(`${name}.mht`)
            fs.writeFileSync(file, snapshot.data ?? '')
          } finally {
            await cdp.detach().catch(() => {})
          }
        } else {
          file = ctx.file(`${name}.txt`)
          fs.writeFileSync(file, await W.ariaOf(p.locator('body'), Number.MAX_SAFE_INTEGER))
        }
      })
      ctx.addFile({ path: file, role: 'value', kind: args.kind, name })
      return null
    },
  })

  define({
    name: 'capture.har',
    run: async (args, ctx) => {
      const rec = await W.ensureHar()
      const key = ctx.session?.key ?? 'main'
      const from = args.whole ? 0 : harMarks.get(key) ?? 0
      harMarks.set(key, rec.order.length)
      const name = args.name ?? 'har'
      const file = ctx.file(`${name}.har`)
      fs.writeFileSync(file, JSON.stringify(W.harDocument(rec, from)))
      ctx.addFile({ path: file, role: 'value', kind: 'har', name })
      return null
    },
  })

  define({
    name: 'capture.record',
    enter: async (args) => {
      const p = page()
      if (!args.from_here) {
        await p.reload({ waitUntil: 'domcontentloaded' }).catch(() => {})
        await W.settle(p, { timeout: 10_000 })
      }
      const token = `r${crypto.randomBytes(4).toString('hex')}`
      await markAll(p, args.hide, 'data-ehx-hide', token)
      let focus
      if (args.focus) {
        await markAll(p, [args.focus], 'data-ehx-focus', token)
        focus = `[data-ehx-focus="${token}"]`
      }
      await W.handlers.videoStart({
        name: args.name,
        viewport: args.viewport?.mobile ? 'mobile' : 'desktop',
        focus,
        pace: camelPace(args.pace),
        fromHere: true,
        noPointer: args.pointer === false,
        hide: args.hide?.length ? [`[data-ehx-hide="${token}"]`] : [],
      })
      boxSteps = []
      await snapRecording(null)
      return null
    },
    exit: async (args, ctx) => {
      if (!W.session.recording) throw new OpError('driver_error', `capture.record ${args.name}: the recording is not running (it was stopped or the page closed)`)
      const crop = W.session.recording.crop ?? null
      const result = await W.handlers.videoStop({ trim: args.trim ?? undefined, name: args.name })
      const chosen = result.trimmed ?? result.file
      const target = ctx.file(`${args.name}${path.extname(chosen) || '.mp4'}`)
      fs.renameSync(chosen, target)
      if (result.trimmed && result.file && result.file !== chosen) fs.rmSync(result.file, { force: true })
      ctx.addFile({ path: target, role: 'value', kind: 'video', name: args.name })
      if (result.interactionsLog && fs.existsSync(result.interactionsLog)) {
        const sidecar = ctx.file(`${args.name}.interactions.json`)
        fs.renameSync(result.interactionsLog, sidecar)
        ctx.addFile({ path: sidecar, role: 'evidence', kind: 'file', name: `${args.name}.interactions.json`, caption: 'interactions of the recording' })
        result.interactionsLog = sidecar
      }
      const boxes = recordingBoxes(args.name, target, result, crop)
      if (boxes) {
        const file = ctx.file(`${args.name}.boxes.json`)
        fs.writeFileSync(file, JSON.stringify(boxes))
        ctx.addFile({ path: file, role: 'evidence', kind: 'file', name: `${args.name}.boxes.json`, caption: 'box maps of the recording steps' })
      }
      ctx.detail = { durationSeconds: result.durationSeconds, warnings: result.warnings ?? [], firstFrameMs: result.firstFrameMs }
      return null
    },
  })

  define({
    name: 'capture.hold',
    run: async (args) => {
      if (W.session.recording) await sleep(args.duration)
      return null
    },
  })

  async function anchorFind(p, target, key, ctx) {
    const found = await resolve(p, target, { timeout: deadlineFor({}, ctx, { fallback: 5_000 }) })
    if (found.point) throw new OpError('target_unsupported', 'overlays anchor to elements, not points', { detail: { target } })
    await found.locator.scrollIntoViewIfNeeded({ timeout: 5_000 }).catch(() => {})
    await found.locator.evaluate((el, name) => {
      const keys = new Set((el.getAttribute('data-ehx-anno') || '').split(/\s+/).filter(Boolean))
      keys.add(name)
      el.setAttribute('data-ehx-anno', [...keys].join(' '))
    }, key)
    return W.annoFind(found.spec)
  }

  function imageSource(image) {
    if (!image?.path) throw new OpError('args_invalid', 'the image overlay needs an ingested image file')
    const data = fs.readFileSync(image.path)
    return `data:${image.mime || 'image/png'};base64,${data.toString('base64')}`
  }

  define({
    name: 'overlay.show',
    run: async (args, ctx) => {
      const p = page()
      const overlay = args.overlay
      const kind = overlay.kind
      const key = overlay.key ?? `${kind}${++annoCounter}`
      const side = overlay.placement?.side ?? 'auto'
      const show = (method, payload, text) => W.annoShow(p, { method, args: payload, key, hold: overlay.hold ?? undefined, keep: overlay.keep, text })
      switch (kind) {
        case 'title':
          await show('title', { title: overlay.text, subtitle: overlay.subtitle, kicker: overlay.kicker }, overlay.text)
          break
        case 'caption':
          await show('caption', { text: overlay.text, step: overlay.step, position: overlay.position }, overlay.text)
          break
        case 'callout':
          await show('callout', { text: overlay.text, placement: side, find: await anchorFind(p, overlay.anchor, key, ctx) }, overlay.text)
          break
        case 'highlight':
          await show('highlight', { shape: overlay.shape, find: await anchorFind(p, overlay.anchor, key, ctx) })
          break
        case 'spotlight': {
          const find = await anchorFind(p, overlay.anchor, key, ctx)
          await W.annoCall(p, 'spotlight', { key, find, dim: overlay.dim ?? undefined })
          if (overlay.text) await W.annoCall(p, 'callout', { key: `${key}-text`, text: overlay.text, placement: side, find })
          if (overlay.keep) W.keptAnnotations.set(key, { method: 'spotlight', args: { find, dim: overlay.dim ?? undefined } })
          W.annoRecord({ kind: 'spotlight', key, text: overlay.text ?? null })
          const wait = overlay.hold ?? (W.session.recording ? W.readMs(overlay.text) : 0)
          if (wait > 0) await sleep(wait)
          if (!overlay.keep) {
            await W.annoCall(p, 'remove', key).catch(() => {})
            if (overlay.text) await W.annoCall(p, 'remove', `${key}-text`).catch(() => {})
          }
          break
        }
        case 'arrow': {
          const find = await anchorFind(p, overlay.anchor, key, ctx)
          const toFind = await anchorFind(p, overlay.to, `${key}-to`, ctx)
          await show('arrow', { find, toFind, toKey: `${key}-to`, text: overlay.text }, overlay.text)
          break
        }
        case 'blur':
          await show('blur', { radius: overlay.radius ?? 8, find: await anchorFind(p, overlay.anchor, key, ctx) })
          break
        case 'redact':
          await show('redact', { find: await anchorFind(p, overlay.anchor, key, ctx) })
          break
        case 'icon': {
          const glyph = ICONS[overlay.name]
          if (!glyph) throw new OpError('args_invalid', `unknown overlay icon ${overlay.name}; the set is ${Object.keys(ICONS).join(', ')}`)
          await show('icon', { glyph, find: await anchorFind(p, overlay.anchor, key, ctx) })
          break
        }
        case 'image':
          await show('image', { src: imageSource(overlay.image), width: overlay.width ?? 160, find: await anchorFind(p, overlay.anchor, key, ctx) })
          break
        case 'step':
          await show('step', { index: overlay.index ?? annoCounter, find: await anchorFind(p, overlay.anchor, key, ctx) })
          break
        default:
          throw new OpError('op_unsupported', `the overlay ${kind} has no live renderer on web`, { detail: { kind } })
      }
      if (overlay.keep) await sleep(OVERLAY_FADE_MS)
      return null
    },
  })

  define({
    name: 'overlay.remove',
    run: async (args) => {
      const p = page()
      for (const key of [args.key, `${args.key}-text`, `${args.key}-to`]) {
        W.keptAnnotations.delete(key)
        await W.annoCall(p, 'remove', key).catch(() => {})
      }
      return null
    },
  })

  define({
    name: 'overlay.clear',
    run: async (args) => {
      for (const [key, entry] of [...W.keptAnnotations]) if (!args.kind || entry.method === args.kind) W.keptAnnotations.delete(key)
      await W.annoCall(page(), 'clear', args.kind ?? undefined)
      return null
    },
  })

  define({
    name: 'inject.script',
    run: async (args) => {
      if ((args.when ?? 'now') === 'init') {
        await W.addInitScript(args.source)
        return null
      }
      await page().evaluate(`(async () => { ${args.source}\n})()`)
      return null
    },
  })

  define({
    name: 'inject.style',
    run: async (args) => {
      const install = `(() => { const css = ${JSON.stringify(args.css)}; const add = () => { const style = document.createElement('style'); style.setAttribute('data-ehx-injected', ''); style.textContent = css; (document.head || document.documentElement).appendChild(style) }; if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', add, { once: true }); else add() })()`
      await W.addInitScript(install)
      await page().evaluate(install)
      return null
    },
  })

  define({
    name: 'inject.eval',
    run: async (args) => {
      const source = String(args.source)
      const body = /\breturn\b|;|\n/.test(source) ? source : `return (${source})`
      const value = await page().evaluate(`(async (arg) => { ${body}\n})(${JSON.stringify(args.arg ?? null)})`)
      return value === undefined ? null : JSON.parse(JSON.stringify(value))
    },
  })

  async function installRoute(entry) {
    for (const context of W.contexts()) await context.route(entry.matcher, entry.handler)
    routes.push(entry)
  }

  async function removeRoute(entry) {
    for (const context of W.contexts()) await context.unroute(entry.matcher, entry.handler).catch(() => {})
    const index = routes.indexOf(entry)
    if (index >= 0) routes.splice(index, 1)
  }

  define({
    name: 'net.route',
    run: async (args) => {
      const action = args.action
      const method = args.method ?? '*'
      let used = 0
      const entry = { pattern: args.pattern, matcher: urlMatcher(args.pattern) }
      entry.handler = async (route) => {
        const request = route.request()
        if (method !== '*' && request.method() !== method) return route.fallback()
        if (args.times && used >= args.times) return route.fallback()
        used++
        if (args.times && used >= args.times) setTimeout(() => removeRoute(entry), 0)
        switch (action.kind) {
          case 'delay':
            await sleep(action.by)
            return route.fallback()
          case 'respond': {
            const headers = { ...(action.headers ?? {}) }
            if (action.json !== null && action.json !== undefined) return route.fulfill({ status: action.status ?? 200, headers, json: action.json })
            return route.fulfill({ status: action.status ?? 200, headers, body: action.body ?? '' })
          }
          case 'patch': {
            const response = await route.fetch()
            let json
            try {
              json = await response.json()
            } catch {
              return route.fulfill({ response })
            }
            return route.fulfill({ response, json: setPath(json, action.path, action.value) })
          }
          case 'abort':
            return route.abort(action.reason ?? 'failed')
          default:
            return route.continue()
        }
      }
      await installRoute(entry)
      return null
    },
  })

  define({
    name: 'net.unroute',
    run: async (args) => {
      for (const entry of [...routes]) if (!args.pattern || entry.pattern === args.pattern) await removeRoute(entry)
      return null
    },
  })

  define({
    name: 'net.shape',
    run: async (args) => {
      const profile = args.profile
      const p = page()
      const cdp = await W.cdpFor(p)
      await cdp.send('Network.enable')
      await cdp.send('Network.emulateNetworkConditions', {
        offline: Boolean(profile.offline),
        latency: profile.latency ?? 0,
        downloadThroughput: profile.down_kbps ? (profile.down_kbps * 1000) / 8 : -1,
        uploadThroughput: profile.up_kbps ? (profile.up_kbps * 1000) / 8 : -1,
      })
      if (shape) await removeRoute(shape)
      shape = null
      if (profile.loss && profile.loss > 0) {
        const every = Math.max(1, Math.round(1 / profile.loss))
        let seen = 0
        shape = { pattern: '**', matcher: () => true }
        shape.handler = async (route) => {
          seen++
          if (seen % every === 0) return route.abort('failed')
          return route.fallback()
        }
        await installRoute(shape)
      }
      return null
    },
  })

  function requestKey(request) {
    const body = request.postDataBuffer()
    const digest = body ? crypto.createHash('sha256').update(body).digest('hex').slice(0, 16) : ''
    return `${request.method()} ${request.url()} ${digest}`
  }

  define({
    name: 'net.cassette',
    enter: async (args, ctx) => {
      const store = ctx.resources?.cassette
      if (!store?.dir) throw new OpError('driver_error', `net.cassette ${args.name}: the hub gave no cassette directory for this run`)
      if (cassette) throw new OpError('driver_error', `net.cassette ${args.name}: cassette ${cassette.name} is still open`)
      const file = path.join(store.dir, 'cassette.json')
      const mode = store.mode === 'replay' || (store.mode === 'auto' && fs.existsSync(file)) ? 'replay' : 'record'
      const state = { name: args.name, mode, file, entries: [], misses: [], queues: new Map() }
      if (mode === 'replay') {
        if (!fs.existsSync(file)) throw new OpError('driver_error', `net.cassette ${args.name}: no recording at ${file}`)
        for (const entry of JSON.parse(fs.readFileSync(file, 'utf8')).entries ?? []) {
          if (!state.queues.has(entry.key)) state.queues.set(entry.key, [])
          state.queues.get(entry.key).push(entry)
        }
      }
      state.route = {
        pattern: '**',
        matcher: (url) => W.isAppUrl(url.href),
        handler: async (route) => {
          const request = route.request()
          const key = requestKey(request)
          if (state.mode === 'replay') {
            const queue = state.queues.get(key)
            const entry = queue?.length > 1 ? queue.shift() : queue?.[0]
            if (!entry) {
              state.misses.push(key)
              return route.abort('failed')
            }
            return route.fulfill({ status: entry.status, headers: entry.headers, body: Buffer.from(entry.body, 'base64') })
          }
          const response = await route.fetch()
          const body = await response.body()
          state.entries.push({ key, method: request.method(), url: request.url(), status: response.status(), headers: response.headers(), body: body.toString('base64') })
          return route.fulfill({ response, body })
        },
      }
      await installRoute(state.route)
      cassette = state
      return null
    },
    exit: async (args, ctx) => {
      const state = cassette
      cassette = null
      if (!state) return null
      await removeRoute(state.route)
      if (state.mode === 'record') {
        fs.mkdirSync(path.dirname(state.file), { recursive: true })
        fs.writeFileSync(state.file, JSON.stringify({ name: state.name, entries: state.entries }))
      }
      ctx.detail = { cassette: { mode: state.mode, entries: state.entries.length, misses: state.misses.slice(0, 20) } }
      if (state.misses.length) {
        throw new OpError('driver_error', `net.cassette ${state.name}: ${state.misses.length} request(s) were not in the cassette, first ${state.misses[0]}`, { detail: { misses: state.misses.slice(0, 20) } })
      }
      return null
    },
  })

  define({
    name: 'web.logs',
    run: async (args) => (await W.handlers.logs({ kind: LOG_KINDS[args.kind ?? 'console'] ?? 'console', limit: args.limit ?? 100 })).entries,
  })

  define({ name: 'web.clear_logs', run: async () => { await W.handlers.clearLogs(); return null } })

  define({
    name: 'web.viewport',
    run: async (args) => {
      const profile = W.viewports[args.name]
      if (!profile) throw new OpError('args_invalid', `unknown viewport ${args.name}; this worker has ${Object.keys(W.viewports).join(', ')}`)
      await page().setViewportSize(profile.viewport)
      await W.settle(page(), { timeout: 5_000 })
      return null
    },
  })

  async function snapshot({ kinds = ['screen'], outDir }) {
    const files = []
    if (!outDir) return { files }
    fs.mkdirSync(outDir, { recursive: true })
    const p = page()
    if (!p || p.isClosed()) return { files }
    const stamp = Date.now()
    for (const kind of kinds) {
      try {
        if (kind === 'screen') {
          const file = path.join(outDir, `screen-${stamp}.png`)
          await capturePng(p, file, {})
          files.push({ path: file, role: 'evidence', kind: 'screenshot', name: path.basename(file), caption: `screen at ${p.url()}` })
        } else if (kind === 'dom') {
          const file = path.join(outDir, `dom-${stamp}.html`)
          fs.writeFileSync(file, await p.content())
          files.push({ path: file, role: 'evidence', kind: 'dom', name: path.basename(file) })
        } else if (kind === 'a11y') {
          const file = path.join(outDir, `a11y-${stamp}.txt`)
          fs.writeFileSync(file, `${p.url()}\n${await W.ariaOf(p.locator('body'), W.treeMaxLines)}`)
          files.push({ path: file, role: 'evidence', kind: 'a11y', name: path.basename(file) })
        }
      } catch (error) {
        W.log(`snapshot ${kind} failed: ${error?.message ?? error}`)
      }
    }
    return { files }
  }

  return { snapshot }
}
