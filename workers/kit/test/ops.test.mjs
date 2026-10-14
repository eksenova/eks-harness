import fs from 'node:fs'
import path from 'node:path'
import { describe, expect, it, vi } from 'vitest'
import { watchOwner } from '../src/lifetime.mjs'
import { OpError, createOpRegistry } from '../src/ops.mjs'
import { validate } from '../src/schema.mjs'
import { strictPick, toMobileSpec, toWebSpec } from '../src/targets.mjs'
import { tempDir } from '../src/testing.mjs'

const SCHEMAS = {
  ops: {
    'ui.tap': { $ref: '#/$defs/Tap' },
  },
  $defs: {
    Target: {
      type: 'object',
      properties: { by: { type: 'string' }, args: { type: 'object' }, nth: { type: ['integer', 'null'] } },
      required: ['by'],
      additionalProperties: false,
    },
    Tap: {
      type: 'object',
      properties: {
        target: { $ref: '#/$defs/Target' },
        timeout: { anyOf: [{ type: 'integer' }, { type: 'null' }] },
        force: { type: 'boolean' },
      },
      required: ['target'],
      additionalProperties: false,
    },
  },
}

describe('schema', () => {
  it('accepts valid args and names every problem in invalid ones', () => {
    const ok = { target: { by: 'test_id', args: { id: 'x' } }, timeout: 500, force: false }
    expect(validate(SCHEMAS.ops['ui.tap'], ok, SCHEMAS.$defs)).toEqual([])
    const errors = validate(SCHEMAS.ops['ui.tap'], { target: { args: {} }, timeout: '5s', extra: 1 }, SCHEMAS.$defs)
    expect(errors).toContain('target: missing by')
    expect(errors.some((e) => e.startsWith('timeout:'))).toBe(true)
    expect(errors).toContain('args: unknown field extra')
  })

  it('checks tuples, enums and nested arrays', () => {
    const schema = { type: 'array', prefixItems: [{ type: 'integer' }, { enum: ['a', 'b'] }], minItems: 2, maxItems: 2 }
    expect(validate(schema, [1, 'a'])).toEqual([])
    expect(validate(schema, [1, 'c'])[0]).toMatch(/is not one of/)
    expect(validate(schema, [1])[0]).toMatch(/at least 2/)
  })
})

describe('op registry', () => {
  it('runs an op, collects files and returns its value', async () => {
    const registry = createOpRegistry({ platform: 'web', schemas: SCHEMAS })
    registry.define({
      name: 'ui.tap',
      run: async (args, ctx) => {
        const file = ctx.file('tap.txt')
        fs.writeFileSync(file, 'x')
        ctx.addFile({ path: file, role: 'evidence', kind: 'log' })
        return { tapped: args.target.args.id, node: ctx.site.node }
      },
    })
    const outDir = path.join(tempDir(), 'out')
    const reply = await registry.handle({ op: 'ui.tap', args: { target: { by: 'test_id', args: { id: 'go' } } }, site: { node: 'n1' }, outDir })
    expect(reply.ok).toBe(true)
    expect(reply.value).toEqual({ tapped: 'go', node: 'n1' })
    expect(reply.files).toHaveLength(1)
    expect(reply.files[0]).toMatchObject({ role: 'evidence', kind: 'log' })
  })

  it('answers unknown ops, invalid args, typed errors and deadlines', async () => {
    const registry = createOpRegistry({ platform: 'web', schemas: SCHEMAS })
    registry.define({ name: 'ui.tap', run: async () => { throw new OpError('target_not_found', 'none', { retryable: true }) } })
    registry.define({ name: 'ui.slow', run: () => new Promise((resolve) => setTimeout(resolve, 200)) })
    registry.define({ name: 'ui.boom', run: () => { throw new Error('kaput') } })
    expect((await registry.handle({ op: 'ui.nope' })).error.code).toBe('op_unsupported')
    expect((await registry.handle({ op: 'ui.tap', args: {} })).error.code).toBe('args_invalid')
    const typed = await registry.handle({ op: 'ui.tap', args: { target: { by: 'text', args: { text: 'a' } } } })
    expect(typed.error).toMatchObject({ code: 'target_not_found', retryable: true })
    const late = await registry.handle({ op: 'ui.slow', deadlineMs: Date.now() + 20 })
    expect(late.error.code).toBe('timeout')
    expect((await registry.handle({ op: 'ui.boom' })).error).toMatchObject({ code: 'driver_error', message: 'kaput' })
  })

  it('skips ops of other platforms and needs exit with enter', () => {
    const registry = createOpRegistry({ platform: 'ios' })
    expect(registry.define({ name: 'web.wheel', platforms: ['web'], run: () => null })).toBe(false)
    expect(registry.names()).toEqual([])
    expect(() => registry.define({ name: 'capture.record', enter: () => null })).toThrow(/exit/)
  })

  it('routes container phases', async () => {
    const registry = createOpRegistry({ platform: 'web' })
    const seen = []
    registry.define({ name: 'capture.record', enter: () => seen.push('enter'), exit: () => { seen.push('exit'); return 'video' } })
    await registry.handle({ op: 'capture.record', phase: 'enter' })
    const done = await registry.handle({ op: 'capture.record', phase: 'exit' })
    expect(seen).toEqual(['enter', 'exit'])
    expect(done.value).toBe('video')
    expect((await registry.handle({ op: 'capture.record', phase: 'run' })).error.code).toBe('op_unsupported')
  })
})

describe('targets', () => {
  it('translates targets for web and mobile', () => {
    const target = { by: 'text', args: { text: 'Kaydet', exact: true }, within: { by: 'role', args: { role: 'dialog' } }, nth: 1 }
    expect(toWebSpec(target)).toEqual({ text: 'Kaydet', exact: true, within: { role: 'dialog', name: undefined, exact: false }, nth: 1 })
    expect(toMobileSpec({ by: 'test_id', args: { id: 'go' }, near: { by: 'text', args: { text: 'x' } } }))
      .toEqual({ testID: 'go', near: { text: 'x', exact: true } })
    expect(toMobileSpec({ by: 'role', args: { role: 'button', name: 'OK' } })).toEqual({ kind: 'button', label: 'OK', exact: false })
    expect(() => toMobileSpec({ by: 'css', args: { selector: 'a' } }, 'ios')).toThrow(/not supported on ios/)
  })

  it('picks strictly', () => {
    const target = { by: 'text', args: { text: 'a' } }
    expect(strictPick(1, target)).toBe(0)
    expect(() => strictPick(0, target)).toThrow(/no element/)
    expect(() => strictPick(2, target, ['a', 'a'])).toThrow(/2 elements match/)
    expect(strictPick(3, { ...target, nth: -1 })).toBe(2)
    expect(() => strictPick(2, { ...target, nth: 5 })).toThrow(/asks for match 5/)
  })
})

describe('lifetime', () => {
  it('exits when the owner is gone, when re-parented and when idle', () => {
    vi.useFakeTimers()
    try {
      const exits = []
      let ownerAlive = true
      const watch = watchOwner({ ownerPid: 42, idleMs: 0, exit: (reason) => exits.push(reason), isAlive: () => ownerAlive, ppid: () => 1 })
      vi.advanceTimersByTime(1_500)
      expect(exits).toEqual([])
      ownerAlive = false
      vi.advanceTimersByTime(1_000)
      expect(exits[0]).toMatch(/owner process 42 is gone/)
      watch.stop()

      const orphaned = []
      watchOwner({ exit: (reason) => orphaned.push(reason), ppid: () => 1 })
      vi.advanceTimersByTime(1_000)
      expect(orphaned[0]).toMatch(/re-parented/)

      let clock = 0
      const idle = []
      const idler = watchOwner({ idleMs: 5_000, exit: (reason) => idle.push(reason), ppid: () => 7, now: () => clock })
      clock = 4_000
      idler.touch()
      clock = 8_000
      vi.advanceTimersByTime(1_000)
      expect(idle).toEqual([])
      clock = 9_500
      vi.advanceTimersByTime(1_000)
      expect(idle[0]).toMatch(/idle/)
    } finally {
      vi.useRealTimers()
    }
  })
})
