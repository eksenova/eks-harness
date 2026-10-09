import { spawn } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'
import { fakeDaemon, startWorker, tempDir } from '../../kit/src/testing.mjs'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const SERVER = path.resolve(HERE, '../src/server.mjs')
const REPO = path.resolve(HERE, '../../..')
const PYTHON = process.env.EHX_TEST_PYTHON || path.join(REPO, '.venv/bin/python')
const { chromium } = createRequire(path.join(HERE, '../package.json'))('playwright')

const PAGE = `<!doctype html><html><head><title>Demo</title></head><body>
<main><h1>Demo app</h1>
<label>Name <input id="name"></label>
<button onclick="document.getElementById('out').textContent = 'Hello ' + document.getElementById('name').value; localStorage.setItem('greeted', '1')">Save</button>
<p id="out"></p><a href="/app/next">Next page</a></main></body></html>`

function chromeBinary() {
  const candidates = [process.env.EHX_TEST_CHROME, chromium.executablePath()]
  const cache = path.join(process.env.HOME ?? '', 'Library/Caches/ms-playwright')
  if (fs.existsSync(cache)) {
    for (const dir of fs.readdirSync(cache).filter((name) => /^chromium-\d+$/.test(name)).sort().reverse()) {
      candidates.push(path.join(cache, dir, 'chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing'))
      candidates.push(path.join(cache, dir, 'chrome-linux64/chrome'), path.join(cache, dir, 'chrome-linux/chrome'))
    }
  }
  candidates.push('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', '/usr/bin/chromium', '/usr/bin/google-chrome')
  return candidates.find((file) => file && fs.existsSync(file)) ?? null
}

const CHROME = chromeBinary()

async function launchChrome() {
  const dir = tempDir('ehx-chrome-')
  const child = spawn(CHROME, ['--headless=new', '--remote-debugging-port=0', `--user-data-dir=${dir}`,
    '--no-first-run', '--no-default-browser-check', 'about:blank'], { stdio: 'ignore' })
  const file = path.join(dir, 'DevToolsActivePort')
  for (let i = 0; i < 200 && !fs.existsSync(file); i++) await new Promise((r) => setTimeout(r, 50))
  const port = fs.readFileSync(file, 'utf8').split('\n')[0]
  return { child, cdp: `http://127.0.0.1:${port}` }
}

describe.skipIf(!CHROME)('web driver worker', () => {
  let chrome
  let daemon
  let worker
  const uploads = []

  beforeAll(async () => {
    chrome = await launchChrome()
    daemon = await fakeDaemon({
      'GET /app(/.*)?': () => ({ raw: PAGE }),
      'POST /api/leases/sid1/browser': () => ({ cdp: chrome.cdp }),
      'POST /api/leases/sid1/heartbeat': () => ({ ok: true }),
      'POST /api/leases/sid1/meta': () => ({ ok: true }),
      'POST /api/artifacts': ({ body }) => {
        const text = body.toString('latin1')
        const kind = /name="kind"\r\n\r\n([^\r]+)/.exec(text)?.[1]
        uploads.push(kind)
        return { id: `a${uploads.length}`, kind, rawUrl: `http://store/raw/${uploads.length}`, url: 'http://store/ui', sessionUrl: 'http://store/s' }
      },
      'GET /api2/me': ({ req }) => ({ auth: req.headers.authorization ?? null }),
    })
    const ext = path.join(tempDir(), 'ext.mjs')
    fs.writeFileSync(ext, `export default function extend(kit) {
      let token = kit.restore(kit.id)?.token ?? null
      return {
        helpers: { signIn: async (name) => { token = 't-' + name; kit.persist(kit.id, { token }); return token } },
        routes: { '/acme/token': async () => ({ token }) },
        apiHeaders: async () => (token ? { Authorization: 'Bearer ' + token } : {}),
        status: async () => ({ signedIn: Boolean(token) }),
      }
    }`)
    worker = await startWorker(SERVER, {
      baseUrl: `${daemon.url}/app`, sid: 'sid1', apiBaseUrl: `${daemon.url}/api2`, locale: 'en-GB',
      viewports: { desktop: { width: 900, height: 700 }, mobile: { width: 390, height: 844, deviceScaleFactor: 2 } },
      encoder: [PYTHON, '-m', 'eks_harness.capture.encode'], extensions: [{ id: 'acme', module: ext }],
      pace: { moveMs: 0, dwellMs: 0, holdMs: 0, screenHoldMs: 0 }, options: { heartbeatMs: 300 },
    }, { env: { EKS_HARNESS_URL: daemon.url, EKS_HARNESS_API_KEY: '' }, timeout: 60_000 })
  }, 90_000)

  afterAll(async () => {
    await worker?.stop()
    chrome?.child.kill('SIGKILL')
    await daemon?.close()
  })

  it('drives the page through a script and captures both viewports', async () => {
    const run = await worker.post('/exec', { script: `
      await goto('/')
      await fill('#name', 'Ada')
      await click('Save')
      const greeting = await text('#out')
      const shots = await shot('after-save')
      return { greeting, shots: shots.map((s) => s.viewport + ':' + s.id), title: await evaluate(() => document.title) }
    ` })
    expect(run.body.ok, JSON.stringify(run.body)).toBe(true)
    expect(run.body.value.greeting).toBe('Hello Ada')
    expect(run.body.value.title).toBe('Demo')
    expect(run.body.value.shots).toEqual(['desktop:a1', 'mobile:a2'])
    expect(run.body.steps.map((s) => s.name)).toEqual(expect.arrayContaining(['goto', 'fill', 'click', 'text', 'shot']))
  })

  it('uses extension helpers, routes, api headers and status', async () => {
    const run = await worker.post('/exec', { script: `const t = await signIn('ada'); return { t, me: await api('GET', '/me') }` })
    expect(run.body.value).toEqual({ t: 't-ada', me: { auth: 'Bearer t-ada' } })
    expect((await worker.post('/acme/token')).body).toEqual({ token: 't-ada' })
    const status = await worker.post('/status')
    expect(status.body).toMatchObject({ up: true, sid: 'sid1', extensions: ['acme'], extensionStatus: { acme: { signedIn: true } } })
    expect(status.body.viewports.desktop).toBe('desktop-900x700')
    const session = JSON.parse(fs.readFileSync(path.join(worker.stateDir, 'session.json'), 'utf8'))
    expect(session.extensions.acme).toEqual({ token: 't-ada' })
  })

  it('streams emitted events and heartbeats the lease', async () => {
    const controller = new AbortController()
    const response = await fetch(worker.base + '/events', { signal: controller.signal })
    const reader = response.body.getReader()
    await worker.post('/exec', { script: `await emit('card-flipped', { side: 'back' }); return 1` })
    let text = ''
    while (!text.includes('card-flipped')) text += new TextDecoder().decode((await reader.read()).value)
    controller.abort()
    expect(JSON.parse(text.split('data: ').pop())).toMatchObject({ source: 'web', name: 'card-flipped', data: { side: 'back' } })
    expect(daemon.calls.filter((c) => c.path === '/api/leases/sid1/heartbeat').length).toBeGreaterThan(0)
    expect((await worker.post('/wait-for', { selector: '#out' })).status).toBe(200)
    expect((await worker.post('/wait')).status).toBe(404)
  })

  it.skipIf(!fs.existsSync(PYTHON))('records the live page into a verified mp4 with a step log', async () => {
    const run = await worker.post('/exec', { script: `
      await video.start('demo', { pace: 'fast', fromHere: true, hold: 300 })
      await annotate.caption('Typing a name', { hold: 300 })
      await fill('#name', 'Grace')
      await click('Save')
      return await video.stop({ hold: 300, trim: true })
    ` })
    expect(run.body.ok, JSON.stringify(run.body)).toBe(true)
    expect(run.body.value.durationSeconds).toBeGreaterThan(0.5)
    expect(uploads.filter((kind) => kind === 'video').length).toBe(2)
  })
})
