import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'
import { fakeDaemon, startWorker, tempDir } from '../../kit/src/testing.mjs'

const SERVER = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../src/server.mjs')

describe('mobile driver worker', () => {
  let daemon
  let worker
  let fixtures

  beforeAll(async () => {
    daemon = await fakeDaemon({ 'POST /api/leases/[^/]+/heartbeat': () => ({ ok: true }) })
    fixtures = tempDir()
    fs.writeFileSync(path.join(fixtures, 'card.png'), Buffer.from([1, 2, 3]))
    const ext = path.join(tempDir(), 'ext.mjs')
    fs.writeFileSync(ext, `export default function extend(kit) {
      return {
        routes: { '/acme/ping': async (body) => ({ pong: body.n + 1, slots: kit.config.fakeSlots }) },
        helpers: (lane) => ({ whoami: async () => lane.platform }),
      }
    }`)
    worker = await startWorker(SERVER, {
      sids: { ios: 'sidios01' }, fixtureDirs: [fixtures], fakeSlots: ['nfc'],
      extensions: [{ id: 'acme', module: ext }], options: { heartbeatMs: 200 },
    }, { env: { EKS_HARNESS_URL: daemon.url, EKS_HARNESS_API_KEY: '' } })
  })

  afterAll(async () => {
    await worker?.stop()
    await daemon?.close()
  })

  it('arms, reports and clears fakes per platform with app-registered slots', async () => {
    expect((await worker.post('/arm', { slot: 'nfc', platform: 'ios', mode: 'error' })).status).toBe(200)
    expect((await worker.post('/arm', { slot: 'boarding-pass', mode: 'success', data: { seat: '3A' } })).status).toBe(200)
    expect((await worker.post('/arm', { slot: 'Bad Slot' })).status).toBe(400)
    const ios = await worker.get('/state?platform=ios')
    expect(ios.body.nfc).toEqual({ mode: 'error' })
    expect(ios.body['boarding-pass'].data.seat).toBe('3A')
    const android = await worker.get('/state?platform=android')
    expect(android.body.nfc).toBeNull()
    await worker.post('/clear', { platform: 'ios', slot: 'nfc' })
    expect((await worker.get('/state?platform=ios')).body.nfc).toBeNull()
  })

  it('serves fixtures as base64 and streams app events', async () => {
    const media = await worker.get('/media-base64/card.png')
    expect(media.body.base64).toBe(Buffer.from([1, 2, 3]).toString('base64'))
    expect((await worker.get('/media-base64/missing.png')).status).toBe(404)
    const controller = new AbortController()
    const response = await fetch(worker.base + '/events', { signal: controller.signal })
    const reader = response.body.getReader()
    await worker.post('/app-events?platform=ios', { event: 'nfc.read', payload: { ok: true } })
    let text = ''
    while (!text.includes('nfc.read')) text += new TextDecoder().decode((await reader.read()).value)
    controller.abort()
    const event = JSON.parse(text.split('data: ').pop())
    expect(event).toMatchObject({ source: 'ios', name: 'nfc.read', data: { ok: true } })
  })

  it('reports health, mounts extension routes and heartbeats its leases', async () => {
    const health = await worker.get('/health')
    expect(health.body).toMatchObject({ ok: true, sids: { ios: 'sidios01' }, extensions: ['acme'] })
    expect(health.body.slots).toEqual(expect.arrayContaining(['nfc', 'boarding-pass']))
    expect((await worker.post('/acme/ping', { n: 1 })).body).toEqual({ pong: 2, slots: ['nfc'] })
    await new Promise((resolve) => setTimeout(resolve, 600))
    expect(daemon.calls.filter((call) => call.path === '/api/leases/sidios01/heartbeat').length).toBeGreaterThan(1)
    const exec = await worker.post('/exec', { script: 'return 1' })
    expect(exec.status).toBe(500)
    expect(exec.body.error).toMatch(/no app is connected|not connected/)
  })
})
