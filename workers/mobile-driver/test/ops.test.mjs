import fs from 'node:fs'
import path from 'node:path'
import { describe, expect, it } from 'vitest'

import { createOpRegistry } from '../../kit/src/ops.mjs'
import { tempDir } from '../../kit/src/testing.mjs'
import { defineMobileOps, strictSpec } from '../src/ops.mjs'

const nodes = [
  { id: 'save', label: 'Kaydet', kind: 'button', value: null, disabled: false },
  { id: null, label: 'Sil', kind: 'button', value: null, disabled: false },
  { id: null, label: 'Sil', kind: 'button', value: null, disabled: true },
  { id: 'name', label: 'Ad', kind: 'input', value: 'Ayşe', disabled: false },
]

function matches(spec) {
  return nodes.filter((node) => (spec.testID === undefined || node.id === spec.testID)
    && (spec.text === undefined || node.label === spec.text)
    && (spec.kind === undefined || node.kind === spec.kind))
}

function fakeBridge() {
  const calls = []
  let cassetteEntries = []
  const bridge = async (cmd, args) => {
    calls.push({ cmd, args })
    const pick = () => {
      const found = matches(args.target)
      if (!found.length) throw Object.assign(new Error(`${cmd}: no element matches`), { code: 'target_not_found', detail: {} })
      if (found.length > 1 && args.target.nth === undefined) throw Object.assign(new Error(`${cmd}: ${found.length} match`), { code: 'target_ambiguous', detail: { count: found.length } })
      return found[args.target.nth ?? 0]
    }
    switch (cmd) {
      case 'match': return { count: matches(args.target).length, candidates: [] }
      case 'inspect': {
        const node = pick()
        return { ...node, text: node.label, checked: null }
      }
      case 'press': pick(); return 'pressed'
      case 'idle': return { ms: 1, timedOut: false }
      case 'route': return { name: 'Invoices', params: { id: 4 } }
      case 'toasts': return []
      case 'annotate': return args.key
      case 'net':
        if (args.op === 'cassette.stop') return { mode: 'record', entries: cassetteEntries, misses: [] }
        if (args.op === 'cassette.start') cassetteEntries = [{ key: 'GET https://x/api ', status: 200, body: '{}' }]
        return { ok: true }
      case 'eval': return { evaluated: args.code }
      case 'boxes': return { window: { width: 390, height: 844 }, pixelRatio: 3, nodes: [{ testId: 'save', kind: 'button', box: [1, 2, 3, 4], parent: null }] }
      default: return null
    }
  }
  return { bridge, calls }
}

function harness() {
  const { bridge, calls } = fakeBridge()
  const lane = { platform: 'ios', rec: null }
  const armed = []
  const M = {
    config: {},
    startedAt: Date.now() - 1000,
    log: () => {},
    bridgeCall: (cmd, args) => bridge(cmd, args),
    laneFor: () => lane,
    pacedBridge: (laneArg, cmd, args) => bridge(cmd, args),
    pressArgs: () => ({}),
    fillInput: async (laneArg, target, text) => bridge('fill', { target, text }),
    typingMs: () => 0,
    readMs: () => 0,
    reload: async () => 'reloaded',
    newRecording: () => ({ steps: [], enrich: [], startedAt: Date.now() }),
    appErrors: () => ['Uncaught TypeError: boom'],
    stageMedia: () => 'staged.png',
    armFake: (platform, slot, spec) => armed.push({ platform, slot, spec }),
    clearFakes: () => {},
  }
  const registry = createOpRegistry({ platform: 'mobile' })
  defineMobileOps(registry.define, M)
  const call = (op, args, extra = {}) => registry.handle({ op, args, session: { key: 'main', platform: 'ios', timeoutMs: 300 }, deadlineMs: Date.now() + 10_000, ...extra })
  return { call, calls, armed }
}

const byText = (text, more = {}) => ({ by: 'text', args: { text, exact: true }, ...more })

describe('mobile ops', () => {
  it('translates targets into strict bridge specs', () => {
    expect(strictSpec({ by: 'text', args: { text: 'Sil' }, within: { by: 'test_id', args: { id: 'list' } }, nth: -1 }, 'ios'))
      .toEqual({ strict: true, text: 'Sil', exact: true, within: { strict: true, testID: 'list' }, nth: -1 })
    expect(strictSpec({ by: 'role', args: { role: 'button', name: 'Kaydet' } }, 'android')).toEqual({ strict: true, kind: 'button', label: 'Kaydet', exact: false })
  })

  it('taps once the target matches and maps bridge errors to codes', async () => {
    const { call, calls } = harness()
    expect((await call('ui.tap', { target: { by: 'test_id', args: { id: 'save' } } })).ok).toBe(true)
    expect(calls.find((entry) => entry.cmd === 'press').args.target).toEqual({ strict: true, testID: 'save' })
    const ambiguous = await call('ui.tap', { target: byText('Sil') })
    expect(ambiguous.error.code).toBe('target_ambiguous')
    const missing = await call('ui.tap', { target: byText('Yok'), timeout: 100 })
    expect(missing.error.code).toBe('target_not_found')
    const native = await call('ui.tap', { target: { by: 'point', args: { x: 1, y: 2 } } })
    expect(native.error.code).toBe('target_unsupported')
  })

  it('asserts eventually and reports the actual value', async () => {
    const { call } = harness()
    const ok = await call('expect.target', { target: { by: 'test_id', args: { id: 'name' } }, assertion: { kind: 'value', equals: 'Ayşe' } })
    expect(ok.value).toEqual({ ok: true, assertion: 'value' })
    const failed = await call('expect.target', { target: byText('Sil', { nth: 1 }), assertion: { kind: 'enabled' }, timeout: 200 })
    expect(failed.error.code).toBe('assertion')
    expect(failed.error.message).toMatch(/disabled/)
  })

  it('runs checkpoint checks and marks unsupported kinds', async () => {
    const { call } = harness()
    const result = await call('check.checkpoint', {
      label: 'list',
      checks: [{ kind: 'page_errors', params: {} }, { kind: 'route', params: { expected: 'Inv*' } }, { kind: 'error_toasts', params: {} }, { kind: 'layout_spill', params: {} }],
    })
    expect(result.value.checks.map((check) => [check.kind, check.ok])).toEqual([['page_errors', false], ['route', true], ['error_toasts', true], ['layout_spill', false]])
    expect(result.value.checks[3].detail.code).toBe('check_unsupported')
    expect(result.value.ok).toBe(false)
  })

  it('draws overlays through the bridge with anchors resolved', async () => {
    const { call, calls } = harness()
    expect((await call('overlay.show', { overlay: { kind: 'icon', anchor: { by: 'test_id', args: { id: 'save' } }, name: 'check', keep: true } })).ok).toBe(true)
    const drawn = calls.filter((entry) => entry.cmd === 'annotate').at(-1).args
    expect(drawn).toMatchObject({ op: 'icon', glyph: '✓', target: { strict: true, testID: 'save' } })
    expect((await call('overlay.show', { overlay: { kind: 'icon', anchor: { by: 'test_id', args: { id: 'save' } }, name: 'nope', keep: true } })).error.code).toBe('args_invalid')
  })

  it('records a cassette into the hub directory and arms fakes with staged media', async () => {
    const { call, armed } = harness()
    const dir = tempDir()
    const resources = { cassette: { dir, mode: 'record' } }
    expect((await call('net.cassette', { name: 'api', mode: 'auto' }, { phase: 'enter', resources })).ok).toBe(true)
    expect((await call('net.cassette', { name: 'api', mode: 'auto' }, { phase: 'exit', resources })).ok).toBe(true)
    expect(JSON.parse(fs.readFileSync(path.join(dir, 'cassette.json'), 'utf8')).entries).toHaveLength(1)
    await call('fake.arm', { slot: 'camera', mode: 'success', media: { path: '/x/card.png', mime: 'image/png', name: 'card.png' } })
    expect(armed).toEqual([{ platform: 'ios', slot: 'camera', spec: { mode: 'success', fixture: 'staged.png' } }])
  })

  it('evaluates expressions with the argument in scope', async () => {
    const { call } = harness()
    const value = (await call('inject.eval', { source: 'arg + 1', arg: 41 })).value
    expect(value.evaluated).toBe('const arg = 41;\nreturn (arg + 1)')
  })

  it('returns bridge box maps for shots and records one per step while recording', async () => {
    const { call } = harness()
    const prepared = await call('_capture.prepare', { hide: [], boxes: true })
    expect(prepared.value.boxes.nodes[0].testId).toBe('save')
    expect((await call('_capture.prepare', { hide: [] })).value).toEqual({ keys: [] })
    expect((await call('_capture.boxes', {})).value.window).toEqual({ width: 390, height: 844 })
    await call('_recording', { state: 'start', name: 'tour' })
    await call('ui.tap', { target: { by: 'test_id', args: { id: 'save' } } })
    const stopped = (await call('_recording', { state: 'stop' })).value
    expect(stopped.boxes.map((step) => step.action)).toEqual([null, 'ui.tap'])
    expect(stopped.boxes[1].t).toBeGreaterThanOrEqual(stopped.boxes[0].t)
    expect(stopped.boxes[1].boxes.nodes).toHaveLength(1)
  })
})
