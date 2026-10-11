import fs from 'node:fs'
import path from 'node:path'
import { describe, expect, it } from 'vitest'
import { resolveTheme, loadWorkerConfig } from '../src/config.mjs'
import { loadExtensions } from '../src/extensions.mjs'
import { createRunner } from '../src/script-runner.mjs'
import { tempDir } from '../src/testing.mjs'

describe('config', () => {
  it('derives rgb channels and reads inline or file config', () => {
    const theme = resolveTheme({ accent: '#336699', surface: '#000000' }, 'de-DE')
    expect(theme.accentRgb).toBe('51,102,153')
    expect(theme.surfaceRgb).toBe('0,0,0')
    expect(theme.locale).toBe('de-DE')
    expect(loadWorkerConfig({ EHX_WORKER_CONFIG: '{"port": 5}' }).port).toBe(5)
    const file = path.join(tempDir(), 'c.json')
    fs.writeFileSync(file, '{"baseUrl": "http://x"}')
    expect(loadWorkerConfig({ EHX_WORKER_CONFIG: file, EHX_WORKER_PORT: '9' })).toEqual({ baseUrl: 'http://x', port: 9 })
  })
})

describe('extensions', () => {
  it('loads factories with settings and keeps known hooks', async () => {
    const dir = tempDir()
    const module = path.join(dir, 'ext.mjs')
    fs.writeFileSync(module, `export default function extend(kit) {
      return { helpers: { greet: () => 'hi ' + kit.settings.name + ' ' + kit.id }, routes: { '/x': () => 1 }, junk: 1 }
    }`)
    const [ext] = await loadExtensions([{ id: 'acme', module, settings: { name: 'ada' } }], { flag: true })
    expect(ext.id).toBe('acme')
    expect(ext.helpers.greet()).toBe('hi ada acme')
    expect(Object.keys(ext)).toEqual(['id', 'helpers', 'routes'])
    const broken = path.join(dir, 'broken.mjs')
    fs.writeFileSync(broken, 'export const nothing = 1')
    await expect(loadExtensions([{ module: broken }], {})).rejects.toThrow(/no default export/)
    expect(await loadExtensions([{ module: broken, required: false }], {})).toEqual([])
  })
})

describe('script runner', () => {
  it('runs a script against helpers and logs steps', async () => {
    const seen = []
    const runner = createRunner({ helpers: () => ({ add: async (a, b) => a + b }), onStep: (entry) => seen.push(entry.name) })
    const result = await runner.exec('const x = await add(2, 3); return x * 2')
    expect(result.ok).toBe(true)
    expect(result.value).toBe(10)
    expect(seen).toContain('add')
  })
})

describe('backend link', () => {
  it('follows generation changes and reads the full info once per change', async () => {
    const { createBackendLink } = await import('../src/backend-link.mjs')
    const changes = []
    let fetches = 0
    const link = createBackendLink({
      initial: { id: 'api@tree:/x', generation: 1, url: 'http://127.0.0.1:5400' },
      fetchInfo: async () => {
        fetches += 1
        return { lease: { id: 'bl_1' }, backend: { id: 'api@tree:/x', generation: 2, status: 'running' },
                 info: { url: 'http://127.0.0.1:5401', credentials: { admin: { password: 'pw' } } } }
      },
      onChange: async (next, previous) => { changes.push([previous?.generation ?? null, next?.generation ?? null]) },
    })
    expect(await link.observe({ id: 'api@tree:/x', generation: 1, status: 'running' })).toBe(false)
    expect(await link.observe({ id: 'api@tree:/x', generation: 2, status: 'starting' })).toBe(false)
    expect(await link.observe({ id: 'api@tree:/x', generation: 2, status: 'running' })).toBe(true)
    expect(link.get().url).toBe('http://127.0.0.1:5401')
    expect(link.get().credentials.admin.password).toBe('pw')
    expect(await link.observe({ id: 'api@tree:/x', generation: 2, status: 'running' })).toBe(false)
    expect(fetches).toBe(1)
    expect(await link.observe(null)).toBe(true)
    expect(link.get()).toBe(null)
    expect(changes).toEqual([[1, 2], [2, null]])
  })
})
