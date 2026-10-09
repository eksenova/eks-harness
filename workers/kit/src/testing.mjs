import { spawn } from 'node:child_process'
import fs from 'node:fs'
import http from 'node:http'
import os from 'node:os'
import path from 'node:path'

export function tempDir(prefix = 'ehx-test-') {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix))
}

export function startWorker(script, config, { env = {}, timeout = 20_000 } = {}) {
  const stateDir = config.stateDir ?? tempDir()
  const child = spawn(process.execPath, [script], {
    env: { ...process.env, ...env, EHX_WORKER_CONFIG: JSON.stringify({ port: 0, stateDir, ...config }) },
    stdio: ['ignore', 'pipe', 'pipe'],
  })
  let output = ''
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      child.kill('SIGKILL')
      reject(new Error(`worker did not start in ${timeout}ms:\n${output}`))
    }, timeout)
    const onData = (chunk) => {
      output += chunk
      for (const line of output.split('\n')) {
        if (!line.startsWith('{')) continue
        try {
          const ready = JSON.parse(line)
          if (ready.ready) {
            clearTimeout(timer)
            const base = `http://127.0.0.1:${ready.port}`
            resolve({
              child, ready, base, stateDir,
              get output() { return output },
              post: (route, body = {}) => fetch(base + route, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).then(async (r) => ({ status: r.status, body: await r.json() })),
              get: (route) => fetch(base + route).then(async (r) => ({ status: r.status, body: await r.json() })),
              stop: () => new Promise((done) => { child.once('exit', done); child.kill('SIGTERM'); setTimeout(() => child.kill('SIGKILL'), 3000).unref() }),
            })
          }
        } catch {}
      }
    }
    child.stdout.on('data', onData)
    child.stderr.on('data', (chunk) => { output += chunk })
    child.once('exit', (code) => {
      clearTimeout(timer)
      reject(new Error(`worker exited with ${code} before it was ready:\n${output}`))
    })
  })
}

export function fakeDaemon(routes = {}) {
  const calls = []
  const server = http.createServer((req, res) => {
    const chunks = []
    req.on('data', (c) => chunks.push(c))
    req.on('end', async () => {
      const url = new URL(req.url, 'http://127.0.0.1')
      calls.push({ method: req.method, path: url.pathname, body: Buffer.concat(chunks) })
      for (const [pattern, handler] of Object.entries(routes)) {
        const [method, route] = pattern.split(' ')
        const match = url.pathname.match(new RegExp(`^${route}$`))
        if (method === req.method && match) {
          const result = await handler({ req, url, match, body: Buffer.concat(chunks) })
          if (result && result.raw !== undefined) {
            res.writeHead(result.status ?? 200, { 'Content-Type': result.type ?? 'text/html' })
            res.end(result.raw)
            return
          }
          res.writeHead(200, { 'Content-Type': 'application/json' })
          res.end(JSON.stringify(result ?? {}))
          return
        }
      }
      res.writeHead(404, { 'Content-Type': 'application/json' })
      res.end(JSON.stringify({ error: 'not_found', message: url.pathname }))
    })
  })
  return new Promise((resolve) => server.listen(0, '127.0.0.1', () => {
    const url = `http://127.0.0.1:${server.address().port}`
    resolve({ url, calls, close: () => new Promise((done) => server.close(done)) })
  }))
}
