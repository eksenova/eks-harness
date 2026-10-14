import crypto from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'

import { ICONS } from '../../kit/src/icons.mjs'
import { OpError } from '../../kit/src/ops.mjs'
import { describeTarget, toMobileSpec } from '../../kit/src/targets.mjs'

const DEFAULT_TIMEOUT = 10_000
const POLL_MS = 80
const MOBILE_PLATFORMS = ['ios', 'android']
const BRIDGE_CODES = new Set(['target_not_found', 'target_ambiguous', 'target_unsupported', 'args_invalid'])
const OVERLAY_FADE_MS = 260

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

export function strictSpec(target, platform) {
  const spec = toMobileSpec(target, platform)
  const mark = (value) => {
    if (!value || typeof value !== 'object') return value
    const out = { strict: true }
    for (const [key, item] of Object.entries(value)) {
      if (key === 'within' || key === 'near') out[key] = mark(item)
      else if (item !== undefined) out[key] = item
    }
    return out
  }
  return mark(spec)
}

function textOutcome(kind, actual, { equals, contains, matches }) {
  const value = actual === null || actual === undefined ? null : String(actual)
  if (equals !== null && equals !== undefined && value !== equals) return `${kind} is ${JSON.stringify(value)}, expected ${JSON.stringify(equals)}`
  if (contains !== null && contains !== undefined && !(value ?? '').includes(contains)) return `${kind} ${JSON.stringify(value)} does not contain ${JSON.stringify(contains)}`
  if (matches !== null && matches !== undefined && !new RegExp(matches).test(value ?? '')) return `${kind} ${JSON.stringify(value)} does not match /${matches}/`
  return null
}

function globToRegExp(glob) {
  return new RegExp(`^${String(glob).split('*').map((part) => part.replace(/[.+?^${}()|[\]\\]/g, '\\$&')).join('.*')}$`)
}

function asOpError(error) {
  if (error instanceof OpError) return error
  if (error?.code && BRIDGE_CODES.has(error.code)) {
    return new OpError(error.code, error.message, { detail: error.detail ?? {}, retryable: error.code === 'target_not_found' })
  }
  return error
}

export function defineMobileOps(define, M) {
  const checkpoints = new Map()
  const initScripts = []
  let cassette = null
  let annoCounter = 0

  const platformOf = (ctx) => {
    const platform = ctx.session?.platform
    if (!MOBILE_PLATFORMS.includes(platform)) throw new OpError('driver_error', `the mobile worker got a call for platform ${platform}`)
    return platform
  }

  const deadlineFor = (args, ctx, fallback = DEFAULT_TIMEOUT) => {
    const wanted = args?.timeout ?? ctx.session?.timeoutMs ?? fallback
    const left = ctx.remaining()
    return left === null ? wanted : Math.max(0, Math.min(wanted, left - 500))
  }

  async function bridge(ctx, cmd, args = {}, timeout = 20_000) {
    try {
      return await M.bridgeCall(cmd, args, { platform: platformOf(ctx), timeout: Math.max(2_000, timeout) })
    } catch (error) {
      throw asOpError(error)
    }
  }

  function spec(ctx, target) {
    if (target?.surface === 'native' || target?.by === 'point' || target?.by === 'rect') {
      throw new OpError('target_unsupported', `${describeTarget(target)} is served by the native layer, not the app bridge`, { detail: { target } })
    }
    return strictSpec(target, platformOf(ctx))
  }

  async function waitMatch(ctx, target, timeout, { gone = false } = {}) {
    const wanted = spec(ctx, target)
    const deadline = Date.now() + timeout
    let last = { count: 0, candidates: [] }
    for (;;) {
      try {
        last = await bridge(ctx, 'match', { target: wanted }, 8_000)
      } catch (error) {
        if (error instanceof OpError && error.code === 'target_not_found' && !gone) last = { count: 0, candidates: [] }
        else throw error
      }
      if (gone ? last.count === 0 : last.count > 0) return { spec: wanted, ...last }
      if (Date.now() >= deadline) break
      await sleep(POLL_MS)
    }
    if (gone) throw new OpError('timeout', `${describeTarget(target)} is still shown after ${timeout} ms`, { detail: { target, count: last.count }, retryable: true })
    throw new OpError('target_not_found', `no element matches ${describeTarget(target)} within ${timeout} ms`, { detail: { target }, retryable: true })
  }

  async function idle(ctx, timeout = 8_000) {
    return bridge(ctx, 'idle', { timeout }, timeout + 5_000)
  }

  async function boxes(ctx) {
    return bridge(ctx, 'boxes', {}, 15_000)
  }

  async function snapRecording(ctx, lane, action) {
    const rec = lane.rec
    if (!rec) return
    const at = (Date.now() - rec.startedAt) / 1000
    const map = await boxes(ctx).catch(() => null)
    if (map && lane.rec === rec) (rec.boxes ??= []).push({ t: Number(at.toFixed(3)), action, boxes: map })
  }

  async function act(ctx, args, target, run) {
    const timeout = deadlineFor(args, ctx)
    const found = target ? await waitMatch(ctx, target, timeout) : null
    const lane = M.laneFor(platformOf(ctx))
    const value = await run(lane, found?.spec ?? null, timeout)
    if (args.settle !== false) await idle(ctx).catch(() => null)
    await snapRecording(ctx, lane, ctx.op)
    return value
  }

  const paced = (ctx, lane, cmd, payload, target, options = {}) =>
    M.pacedBridge(lane, cmd, payload, { target, ...options }).catch((error) => { throw asOpError(error) })

  define({
    name: 'ui.tap',
    run: (args, ctx) => act(ctx, args, args.target, async (lane, target) => {
      await paced(ctx, lane, 'press', { target, ...M.pressArgs(lane) }, target)
      return null
    }),
  })

  define({
    name: 'ui.double_tap',
    run: (args, ctx) => act(ctx, args, args.target, async (lane, target) => {
      await paced(ctx, lane, 'press', { target, ...M.pressArgs(lane) }, target)
      await paced(ctx, lane, 'press', { target, pressMs: 0 }, target, { dwell: false, quiet: true })
      return null
    }),
  })

  define({
    name: 'ui.long_press',
    run: (args, ctx) => act(ctx, args, args.target, async (lane, target) => {
      await paced(ctx, lane, 'press', { target, long: true, pressMs: args.hold ?? undefined }, target)
      return null
    }),
  })

  define({
    name: 'ui.fill',
    run: (args, ctx) => act(ctx, args, args.target, async (lane, target) => {
      await M.fillInput(lane, target, String(args.value ?? ''), { submit: Boolean(args.submit) }, 0).catch((error) => { throw asOpError(error) })
      return null
    }),
  })

  define({
    name: 'ui.clear',
    run: (args, ctx) => act(ctx, args, args.target, async (lane, target) => {
      await M.fillInput(lane, target, '', {}, 0).catch((error) => { throw asOpError(error) })
      return null
    }),
  })

  define({
    name: 'ui.type',
    run: (args, ctx) => act(ctx, args, args.target, async (lane, target) => {
      const perChar = M.typingMs(lane, args.pace ?? undefined, { byDefault: true })
      await M.fillInput(lane, target, String(args.value ?? ''), { submit: Boolean(args.submit) }, perChar).catch((error) => { throw asOpError(error) })
      return null
    }),
  })

  define({
    name: 'ui.key',
    run: (args, ctx) => act(ctx, args, args.target ?? null, async (lane, target) => {
      const key = String(args.key)
      if (key === 'Enter') {
        if (!target) throw new OpError('args_invalid', 'Enter on mobile submits an input: give the input as target')
        await paced(ctx, lane, 'submit', { target }, target)
      } else if (key === 'Back' || key === 'Escape') {
        await paced(ctx, lane, 'goBack', {}, null, { screenChange: true })
      } else {
        throw new OpError('op_unsupported', `the key ${key} has no app equivalent on mobile; Enter submits and Back goes back`, { detail: { key } })
      }
      return null
    }),
  })

  define({
    name: 'ui.select',
    run: (args, ctx) => act(ctx, args, args.target, async (lane, target) => {
      const value = Array.isArray(args.value) ? args.value[0] : args.value
      await bridge(ctx, 'invoke', { target, prop: 'onValueChange', args: [value] })
      return null
    }),
  })

  define({
    name: 'ui.check',
    run: (args, ctx) => act(ctx, args, args.target, async (lane, target) => {
      await paced(ctx, lane, 'toggle', { target, value: args.on !== false, ...M.pressArgs(lane) }, target)
      return null
    }),
  })

  define({
    name: 'ui.choose',
    run: (args, ctx) => act(ctx, args, args.trigger, async (lane, target, timeout) => {
      await paced(ctx, lane, 'press', { target, ...M.pressArgs(lane) }, target)
      await idle(ctx).catch(() => null)
      const option = await waitMatch(ctx, { by: 'text', args: { text: args.option, exact: true } }, timeout)
      await paced(ctx, lane, 'press', { target: option.spec, ...M.pressArgs(lane) }, option.spec)
      return null
    }),
  })

  define({
    name: 'ui.scroll',
    run: (args, ctx) => act(ctx, args, args.target ?? null, async (lane, target, timeout) => {
      const to = args.to
      if (to && typeof to === 'object') {
        const found = await waitMatch(ctx, to, timeout)
        await bridge(ctx, 'pointer', { op: 'move', target: found.spec, ms: 0 })
        await bridge(ctx, 'pointer', { op: 'hide' })
        return null
      }
      if (Array.isArray(args.by)) {
        throw new OpError('target_unsupported', 'scrolling by a pixel offset is a native gesture on mobile; use ui.swipe or a target', { detail: { by: args.by } })
      }
      await bridge(ctx, 'scroll', { target: target ?? undefined, to: to === 'top' || to === 'start' ? 'top' : 'end' })
      return null
    }),
  })

  define({
    name: 'ui.nav',
    run: async (args, ctx) => {
      const lane = M.laneFor(platformOf(ctx))
      try {
        await paced(ctx, lane, 'navigate', { name: args.route, params: args.params ?? undefined }, null, { screenChange: true })
      } catch (error) {
        if (error instanceof OpError) throw error
        throw new OpError('navigation', error?.message ?? String(error), { retryable: true })
      }
      await idle(ctx).catch(() => null)
      return null
    },
  })

  define({
    name: 'ui.back',
    run: async (args, ctx) => {
      const lane = M.laneFor(platformOf(ctx))
      await paced(ctx, lane, 'goBack', {}, null, { screenChange: true }).catch((error) => {
        throw error instanceof OpError ? error : new OpError('navigation', error?.message ?? String(error))
      })
      await idle(ctx).catch(() => null)
      return null
    },
  })

  define({
    name: 'ui.reload',
    run: async (args, ctx) => {
      const lane = M.laneFor(platformOf(ctx))
      await M.reload(lane)
      for (const source of initScripts) await bridge(ctx, 'eval', { code: source })
      return null
    },
  })

  define({
    name: 'ui.settle',
    run: async (args, ctx) => {
      const timeout = deadlineFor(args, ctx)
      const outcome = await idle(ctx, timeout)
      if (outcome?.timedOut && args.strict !== false) {
        throw new OpError('timeout', `the app did not settle within ${timeout} ms`, { detail: outcome, retryable: true })
      }
      return null
    },
  })

  define({
    name: 'ui.wait_for',
    run: async (args, ctx) => {
      const timeout = deadlineFor(args, ctx)
      const state = args.state ?? 'visible'
      if (state === 'hidden') {
        await waitMatch(ctx, args.target, timeout, { gone: true })
        return null
      }
      const found = await waitMatch(ctx, args.target, timeout)
      const node = await bridge(ctx, 'inspect', { target: found.spec })
      if (state === 'enabled') {
        const deadline = Date.now() + timeout
        let current = node
        while (current.disabled && Date.now() < deadline) {
          await sleep(POLL_MS)
          current = await bridge(ctx, 'inspect', { target: found.spec })
        }
        if (current.disabled) throw new OpError('timeout', `${describeTarget(args.target)} stayed disabled for ${timeout} ms`, { retryable: true })
      }
      return null
    },
  })

  define({
    name: 'ui.wait_gone',
    run: async (args, ctx) => {
      await waitMatch(ctx, args.target, deadlineFor(args, ctx), { gone: true })
      return null
    },
  })

  async function located(ctx, args) {
    return waitMatch(ctx, args.target, deadlineFor(args, ctx, 5_000))
  }

  define({
    name: 'ui.text',
    run: async (args, ctx) => {
      if (!args.target) return String(await bridge(ctx, 'text', {}))
      const found = await located(ctx, args)
      return String(await bridge(ctx, 'text', { target: found.spec }))
    },
  })

  define({
    name: 'ui.value',
    run: async (args, ctx) => {
      const found = await located(ctx, args)
      const node = await bridge(ctx, 'inspect', { target: found.spec })
      return node.value ?? ''
    },
  })

  const quickCount = async (ctx, target, timeout) => {
    const deadline = Date.now() + (timeout ?? 0)
    for (;;) {
      const { count } = await bridge(ctx, 'match', { target: spec(ctx, target) }, 8_000)
      if (count > 0 || Date.now() >= deadline) return count
      await sleep(POLL_MS)
    }
  }

  define({ name: 'ui.count', run: (args, ctx) => quickCount(ctx, args.target, args.timeout) })
  define({ name: 'ui.exists', run: async (args, ctx) => (await quickCount(ctx, args.target, args.timeout)) > 0 })
  define({ name: 'ui.visible', run: async (args, ctx) => (await quickCount(ctx, args.target, args.timeout)) > 0 })

  define({
    name: 'ui.tree',
    run: async (args, ctx) => {
      const info = await bridge(ctx, 'tree', { maxLines: args.max_lines ?? undefined })
      if (!args.target) return `route: ${info.route}\n${info.tree}`
      const found = await located(ctx, args)
      return String(await bridge(ctx, 'text', { target: found.spec }))
    },
  })

  define({ name: 'ui.route', run: async (args, ctx) => {
    const route = await bridge(ctx, 'route', {})
    return { name: route.name, params: route.params ?? {} }
  } })

  define({ name: 'ui.state', run: async (args, ctx) => (await bridge(ctx, 'state', { path: args.path ?? undefined })) ?? null })

  define({
    name: 'ui.layout',
    run: async (args, ctx) => {
      const found = await located(ctx, args)
      const box = await bridge(ctx, 'layout', { target: found.spec })
      if (!box) return { x: 0, y: 0, width: 0, height: 0, visible: false, inViewport: false }
      return { ...box, visible: box.width > 0 && box.height > 0, inViewport: true }
    },
  })

  async function assertOnce(ctx, target, assertion) {
    const wanted = spec(ctx, target)
    const { count } = await bridge(ctx, 'match', { target: wanted }, 8_000)
    const kind = assertion.kind
    if (kind === 'visible') return { failure: count ? null : `${describeTarget(target)} is not shown`, actual: count }
    if (kind === 'hidden') return { failure: count ? `${describeTarget(target)} is shown (${count} match)` : null, actual: count }
    if (kind === 'count') {
      const { equals, at_least: atLeast, at_most: atMost } = assertion
      let failure = null
      if (equals !== null && equals !== undefined && count !== equals) failure = `${count} elements match ${describeTarget(target)}, expected ${equals}`
      else if (atLeast !== null && atLeast !== undefined && count < atLeast) failure = `${count} elements match ${describeTarget(target)}, expected at least ${atLeast}`
      else if (atMost !== null && atMost !== undefined && count > atMost) failure = `${count} elements match ${describeTarget(target)}, expected at most ${atMost}`
      return { failure, actual: count }
    }
    if (count === 0) return { failure: `no element matches ${describeTarget(target)}`, actual: null }
    if (kind === 'attr') throw new OpError('target_unsupported', 'attribute assertions are web only; assert text, value or state on mobile')
    const node = await bridge(ctx, 'inspect', { target: wanted })
    if (kind === 'enabled' || kind === 'disabled') {
      const enabled = !node.disabled
      return { failure: enabled === (kind === 'enabled') ? null : `${describeTarget(target)} is ${enabled ? 'enabled' : 'disabled'}`, actual: enabled }
    }
    if (kind === 'checked') {
      const on = assertion.on !== false
      return { failure: Boolean(node.checked) === on ? null : `${describeTarget(target)} is ${node.checked ? 'on' : 'off'}`, actual: node.checked }
    }
    if (kind === 'text') return { failure: textOutcome('text', node.text, assertion), actual: node.text }
    if (kind === 'value') return { failure: textOutcome('value', node.value ?? '', assertion), actual: node.value }
    throw new OpError('args_invalid', `unknown assertion ${kind}`)
  }

  define({
    name: 'expect.target',
    run: async (args, ctx) => {
      const timeout = deadlineFor(args, ctx, 5_000)
      const deadline = Date.now() + timeout
      let last
      for (;;) {
        last = await assertOnce(ctx, args.target, args.assertion)
        if (!last.failure) return { ok: true, assertion: args.assertion.kind }
        if (args.eventually === false || Date.now() >= deadline) break
        await sleep(POLL_MS * 2)
      }
      const message = args.message ? `${args.message}: ${last.failure}` : last.failure
      throw new OpError('assertion', message, { detail: { assertion: args.assertion, actual: last.actual ?? null, target: args.target, soft: Boolean(args.soft) } })
    },
  })

  async function runCheck(ctx, use, since) {
    const params = use.params ?? {}
    const kind = use.kind
    const outcome = (ok, message, detail = {}) => ({ kind, ok, severity: 'error', message, detail })
    if (kind === 'page_errors') {
      const noise = [...(ctx.session?.noise ?? []), ...(params.noise ?? [])]
      const found = M.appErrors(platformOf(ctx), since).filter((line) => !noise.some((needle) => needle && line.includes(needle)))
      return outcome(!found.length, found.length ? `${found.length} app error(s): ${found.slice(-3).join(' | ')}` : 'no app errors', { errors: found.slice(-10) })
    }
    if (kind === 'error_toasts') {
      let texts
      if (params.selector) {
        const wanted = spec(ctx, params.selector)
        const { count } = await bridge(ctx, 'match', { target: wanted })
        texts = count ? [String(await bridge(ctx, 'text', { target: { ...wanted, nth: 0 } }))] : []
      } else {
        texts = await bridge(ctx, 'toasts', { pattern: M.config.alerts ?? undefined })
      }
      return outcome(!texts.length, texts.length ? `error toast: ${texts.join(' | ')}` : 'no error toasts', { toasts: texts })
    }
    if (kind === 'route') {
      const route = await bridge(ctx, 'route', {})
      const expected = String(params.expected ?? '')
      const ok = expected.includes('*') ? globToRegExp(expected).test(route.name) : route.name === expected
      return outcome(ok, ok ? `on ${route.name}` : `on ${route.name}, expected ${expected}`, { route: route.name })
    }
    return { kind, ok: false, severity: 'error', message: `the check ${kind} is not supported on ${platformOf(ctx)} by this worker`, detail: { code: 'check_unsupported' } }
  }

  define({
    name: 'check.checkpoint',
    run: async (args, ctx) => {
      const key = ctx.session?.key ?? 'main'
      const since = checkpoints.get(key) ?? M.startedAt
      const now = Date.now()
      await idle(ctx, 4_000).catch(() => null)
      const checks = []
      for (const use of args.checks ?? []) checks.push(await runCheck(ctx, use, since))
      checkpoints.set(key, now)
      return { label: args.label, viewport: args.viewport?.name ?? ctx.session?.viewport ?? null, ok: checks.every((check) => check.ok || check.severity !== 'error'), checks, shots: [] }
    },
  })

  define({
    name: '_capture.prepare',
    run: async (args, ctx) => {
      await idle(ctx, 4_000).catch(() => null)
      const keys = []
      for (const target of args.hide ?? []) {
        const wanted = spec(ctx, target)
        const { count } = await bridge(ctx, 'match', { target: wanted })
        for (let index = 0; index < count; index++) {
          const key = `hide-${crypto.randomBytes(3).toString('hex')}`
          await bridge(ctx, 'patch', { target: { ...wanted, nth: index }, key, style: { opacity: 0 } })
          keys.push(key)
        }
      }
      if (args.focus) {
        const found = await waitMatch(ctx, args.focus, 5_000)
        await bridge(ctx, 'pointer', { op: 'move', target: found.spec, ms: 0 }).catch(() => null)
        await bridge(ctx, 'pointer', { op: 'hide' }).catch(() => null)
      }
      if (!args.boxes) return { keys }
      return { keys, boxes: await boxes(ctx).catch(() => null) }
    },
  })

  define({
    name: '_capture.boxes',
    run: async (args, ctx) => {
      await idle(ctx, 4_000).catch(() => null)
      return boxes(ctx)
    },
  })

  define({
    name: '_capture.restore',
    run: async (args, ctx) => {
      for (const key of args.keys ?? []) await bridge(ctx, 'unpatch', { key }).catch(() => null)
      return null
    },
  })

  define({
    name: '_recording',
    run: async (args, ctx) => {
      const lane = M.laneFor(platformOf(ctx))
      if (args.state === 'start') {
        lane.rec = M.newRecording(lane, { name: args.name, pace: args.pace ?? undefined, noPointer: args.pointer === false })
        await snapRecording(ctx, lane, null)
        return { startedAt: lane.rec.startedAt }
      }
      const rec = lane.rec
      lane.rec = null
      if (!rec) return { steps: [], annotations: [], boxes: [] }
      await Promise.allSettled(rec.enrich.splice(0))
      await bridge(ctx, 'annotate', { op: 'clear' }).catch(() => null)
      await bridge(ctx, 'pointer', { op: 'hide' }).catch(() => null)
      return { steps: rec.steps, annotations: rec.annotations ?? [], startedAt: rec.startedAt, boxes: rec.boxes ?? [] }
    },
  })

  function imageSource(image) {
    if (!image?.path) throw new OpError('args_invalid', 'the image overlay needs an ingested image file')
    return `data:${image.mime || 'image/png'};base64,${fs.readFileSync(image.path).toString('base64')}`
  }

  define({
    name: 'overlay.show',
    run: async (args, ctx) => {
      const overlay = args.overlay
      const kind = overlay.kind
      const key = overlay.key ?? `${kind}${++annoCounter}`
      const anchor = async (target) => (await waitMatch(ctx, target, deadlineFor({}, ctx, 5_000))).spec
      const payload = { op: kind, key }
      switch (kind) {
        case 'title':
          Object.assign(payload, { title: overlay.text, subtitle: overlay.subtitle ?? undefined, kicker: overlay.kicker ?? undefined })
          break
        case 'caption':
          Object.assign(payload, { text: overlay.text, step: overlay.step ?? undefined, position: overlay.position ?? undefined })
          break
        case 'callout':
          Object.assign(payload, { target: await anchor(overlay.anchor), text: overlay.text, placement: overlay.placement?.side ?? undefined })
          break
        case 'highlight':
        case 'blur':
        case 'redact':
          Object.assign(payload, { target: await anchor(overlay.anchor) })
          break
        case 'spotlight':
          Object.assign(payload, { target: await anchor(overlay.anchor), dim: overlay.dim ?? undefined })
          break
        case 'arrow':
          Object.assign(payload, { target: await anchor(overlay.anchor), to: await anchor(overlay.to), text: overlay.text ?? undefined })
          break
        case 'icon': {
          const glyph = ICONS[overlay.name]
          if (!glyph) throw new OpError('args_invalid', `unknown overlay icon ${overlay.name}; the set is ${Object.keys(ICONS).join(', ')}`)
          Object.assign(payload, { target: await anchor(overlay.anchor), glyph })
          break
        }
        case 'image':
          Object.assign(payload, { target: await anchor(overlay.anchor), src: imageSource(overlay.image), width: overlay.width ?? undefined })
          break
        case 'step':
          Object.assign(payload, { target: await anchor(overlay.anchor), index: overlay.index ?? annoCounter })
          break
        default:
          throw new OpError('op_unsupported', `the overlay ${kind} has no live renderer on mobile`, { detail: { kind } })
      }
      await bridge(ctx, 'annotate', payload, 15_000)
      const text = overlay.text ?? null
      const lane = M.laneFor(platformOf(ctx))
      if (lane.rec) (lane.rec.annotations ??= []).push({ at: Number(((Date.now() - lane.rec.startedAt) / 1000).toFixed(2)), kind, key, text })
      if (kind === 'spotlight' && overlay.text) {
        await bridge(ctx, 'annotate', { op: 'callout', key: `${key}-text`, target: payload.target, text: overlay.text, placement: overlay.placement?.side ?? undefined })
      }
      const wait = overlay.hold ?? (lane.rec ? M.readMs(text) : 0)
      if (wait > 0) await sleep(wait)
      if (!overlay.keep) {
        await bridge(ctx, 'annotate', { op: 'remove', key }).catch(() => null)
        await bridge(ctx, 'annotate', { op: 'remove', key: `${key}-text` }).catch(() => null)
      } else {
        await sleep(OVERLAY_FADE_MS)
      }
      return null
    },
  })

  define({
    name: 'overlay.remove',
    run: async (args, ctx) => {
      for (const key of [args.key, `${args.key}-text`]) await bridge(ctx, 'annotate', { op: 'remove', key }).catch(() => null)
      return null
    },
  })

  define({
    name: 'overlay.clear',
    run: async (args, ctx) => {
      await bridge(ctx, 'annotate', { op: 'clear', kind: args.kind ?? undefined })
      return null
    },
  })

  const evalSource = (source, arg) => {
    const text = String(source)
    const body = /\breturn\b|;|\n/.test(text) ? text : `return (${text})`
    return `const arg = ${JSON.stringify(arg ?? null)};\n${body}`
  }

  define({
    name: 'inject.script',
    run: async (args, ctx) => {
      if ((args.when ?? 'now') === 'init') initScripts.push(args.source)
      await bridge(ctx, 'eval', { code: String(args.source) })
      return null
    },
  })

  define({
    name: 'inject.eval',
    run: async (args, ctx) => {
      const value = await bridge(ctx, 'eval', { code: evalSource(args.source, args.arg) })
      return value === undefined ? null : value
    },
  })

  define({
    name: 'net.route',
    run: async (args, ctx) => {
      await bridge(ctx, 'net', { op: 'route', method: args.method ?? '*', pattern: args.pattern, action: args.action, times: args.times ?? null })
      return null
    },
  })

  define({
    name: 'net.unroute',
    run: async (args, ctx) => {
      await bridge(ctx, 'net', { op: 'unroute', pattern: args.pattern ?? undefined })
      return null
    },
  })

  define({
    name: 'net.shape',
    run: async (args, ctx) => {
      await bridge(ctx, 'net', { op: 'shape', profile: args.profile })
      return null
    },
  })

  define({
    name: 'net.cassette',
    enter: async (args, ctx) => {
      const store = ctx.resources?.cassette
      if (!store?.dir) throw new OpError('driver_error', `net.cassette ${args.name}: the hub gave no cassette directory for this run`)
      const file = path.join(store.dir, 'cassette.json')
      const mode = store.mode === 'replay' || (store.mode === 'auto' && fs.existsSync(file)) ? 'replay' : 'record'
      const entries = mode === 'replay' ? JSON.parse(fs.readFileSync(file, 'utf8')).entries ?? [] : []
      await bridge(ctx, 'net', { op: 'cassette.start', mode, entries })
      cassette = { name: args.name, mode, file }
      return null
    },
    exit: async (args, ctx) => {
      const state = cassette
      cassette = null
      if (!state) return null
      const closed = await bridge(ctx, 'net', { op: 'cassette.stop' })
      if (state.mode === 'record') {
        fs.mkdirSync(path.dirname(state.file), { recursive: true })
        fs.writeFileSync(state.file, JSON.stringify({ name: state.name, entries: closed.entries ?? [] }))
      }
      const misses = closed.misses ?? []
      ctx.detail = { cassette: { mode: state.mode, entries: (closed.entries ?? []).length, misses: misses.slice(0, 20) } }
      if (misses.length) throw new OpError('driver_error', `net.cassette ${state.name}: ${misses.length} request(s) were not in the cassette, first ${misses[0]}`, { detail: { misses: misses.slice(0, 20) } })
      return null
    },
  })

  define({
    name: 'fake.arm',
    run: async (args, ctx) => {
      const spec = { mode: args.mode ?? 'success' }
      if (args.fixture) spec.fixture = args.fixture
      if (args.data !== undefined && args.data !== null) spec.data = args.data
      if (args.media?.path) spec.fixture = M.stageMedia(args.media)
      M.armFake(platformOf(ctx), args.slot, spec)
      return null
    },
  })

  define({
    name: 'fake.clear',
    run: async (args, ctx) => {
      M.clearFakes(platformOf(ctx), args.slot ?? null)
      return null
    },
  })

  define({ name: 'mobile.dispatch', run: async (args, ctx) => { await bridge(ctx, 'dispatch', { action: args.action }); await idle(ctx).catch(() => null); return null } })

  define({
    name: 'mobile.patch',
    run: async (args, ctx) => {
      const found = await located(ctx, args)
      await bridge(ctx, 'patch', { target: found.spec, key: args.key ?? undefined, props: args.props ?? undefined, style: args.style ?? undefined, text: args.text ?? undefined })
      return null
    },
  })

  define({ name: 'mobile.unpatch', run: async (args, ctx) => { await bridge(ctx, 'unpatch', { key: args.key }); return null } })

  define({
    name: 'mobile.mount',
    run: async (args, ctx) => {
      const target = args.target ? (await located(ctx, args)).spec : undefined
      await bridge(ctx, 'inject', { key: args.key ?? undefined, component: args.component, props: args.props ?? undefined, target })
      return null
    },
  })

  define({ name: 'mobile.unmount', run: async (args, ctx) => { await bridge(ctx, 'unpatch', { key: args.key }); return null } })

  define({
    name: 'mobile.invoke',
    run: async (args, ctx) => {
      const found = await located(ctx, args)
      const value = await bridge(ctx, 'invoke', { target: found.spec, prop: args.prop, args: args.args ?? [] })
      await idle(ctx).catch(() => null)
      return value ?? null
    },
  })

  define({
    name: 'mobile.toggle',
    run: (args, ctx) => act(ctx, args, args.target, async (lane, target) => {
      await paced(ctx, lane, 'toggle', { target, value: args.value ?? undefined, ...M.pressArgs(lane) }, target)
      return null
    }),
  })

  define({
    name: 'mobile.submit',
    run: (args, ctx) => act(ctx, args, args.target, async (lane, target) => {
      await paced(ctx, lane, 'submit', { target }, target)
      return null
    }),
  })

  define({
    name: 'mobile.idle',
    run: async (args, ctx) => {
      const timeout = deadlineFor(args, ctx)
      const outcome = await idle(ctx, timeout)
      if (outcome?.timedOut) throw new OpError('timeout', `the app did not go idle within ${timeout} ms`, { detail: outcome, retryable: true })
      return null
    },
  })

  define({ name: 'mobile.route_names', run: async (args, ctx) => bridge(ctx, 'routeNames', {}) })

  define({ name: 'mobile.logout', run: async (args, ctx) => { await bridge(ctx, 'logout', {}, 20_000); await idle(ctx).catch(() => null); return null } })

  define({ name: 'mobile.close_dev_menu', run: async (args, ctx) => { await bridge(ctx, 'closeDevMenu', {}); return null } })

  define({
    name: 'mobile.pointer',
    run: async (args, ctx) => {
      if (!args.target) {
        await bridge(ctx, 'pointer', { op: 'hide' })
        return null
      }
      const found = await located(ctx, args)
      await bridge(ctx, 'pointer', { op: 'move', target: found.spec, ms: args.duration ?? undefined })
      return null
    },
  })

  async function snapshot({ outDir, platform }) {
    const files = []
    if (!outDir || !MOBILE_PLATFORMS.includes(platform)) return { files }
    fs.mkdirSync(outDir, { recursive: true })
    try {
      const info = await M.bridgeCall('snapshot', {}, { platform, timeout: 8_000 })
      const file = path.join(outDir, `a11y-${Date.now()}.txt`)
      fs.writeFileSync(file, `route: ${info.route}\n${info.tree}`)
      files.push({ path: file, role: 'evidence', kind: 'a11y', name: path.basename(file) })
    } catch (error) {
      M.log(`snapshot failed: ${error?.message ?? error}`)
    }
    return { files }
  }

  return { snapshot }
}
