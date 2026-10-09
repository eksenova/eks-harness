import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { spawnSync } from 'node:child_process'
import { fileURLToPath, pathToFileURL } from 'node:url'

const APP_NAME = 'eks-harness'
const DEFAULT_HOST = '127.0.0.1'
const DEFAULT_PORT = 7171
const USER_AGENT = 'eks-harness-worker/1.0'
const SID_FILES = ['sid', 'ios-sid', 'android-sid']

export class HarnessApiError extends Error {
  constructor(status, payload, route) {
    const message = payload?.message || payload?.error || `HTTP ${status}`
    super(`harness daemon ${route}: ${message}`)
    this.status = status
    this.payload = payload ?? {}
    this.route = route
    this.error = payload?.error ?? null
  }
}

export class LeaseReleasedError extends HarnessApiError {
  constructor(status, payload, route) {
    super(status, payload, route)
    const reacquire = payload?.reacquire ? ` Reacquire with: ${payload.reacquire}` : ''
    this.message = `the lease ${payload?.sid ?? ''} is no longer held (${payload?.state ?? 'released'}): ${payload?.message ?? 'its sid is read-only now'}.${reacquire}`
  }
}

export class DaemonUnavailableError extends Error {}

export function configDir(env = process.env) {
  if (env.EKS_HARNESS_CONFIG_DIR) return env.EKS_HARNESS_CONFIG_DIR
  if (env.EKS_HARNESS_HOME) return path.join(env.EKS_HARNESS_HOME, 'config')
  if (process.platform === 'darwin') return path.join(os.homedir(), 'Library', 'Application Support', APP_NAME)
  if (process.platform === 'win32') return path.join(env.APPDATA || path.join(os.homedir(), 'AppData', 'Roaming'), APP_NAME)
  return path.join(env.XDG_CONFIG_HOME || path.join(os.homedir(), '.config'), APP_NAME)
}

function readJson(file) {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'))
  } catch {
    return null
  }
}

export function readConfig(env = process.env) {
  const values = readJson(path.join(configDir(env), 'config.json')) ?? {}
  const host = env.EKS_HARNESS_SERVER_HOST || values['server.host'] || DEFAULT_HOST
  const port = Number(env.EKS_HARNESS_SERVER_PORT || values['server.port'] || DEFAULT_PORT)
  const publicUrl = String(env.EKS_HARNESS_SERVER_PUBLICURL || values['server.publicUrl'] || '').replace(/\/+$/, '')
  return { host, port, publicUrl }
}

export function localUrl({ host, port }) {
  let target = host
  if (['0.0.0.0', '', 'localhost'].includes(target)) target = '127.0.0.1'
  else if (target === '::') target = '::1'
  if (target.includes(':')) target = `[${target}]`
  return `http://${target}:${port}`
}

let cachedBase = null

function urlFromCli() {
  const bin = harnessBinary()
  if (!bin) return null
  const result = spawnSync(bin, ['daemon', 'status', '--json'], { encoding: 'utf8', timeout: 15_000 })
  if (result.status !== 0) return null
  try {
    const data = JSON.parse(result.stdout)
    return data.url ?? data.localUrl ?? null
  } catch {
    return null
  }
}

export function baseUrl(env = process.env) {
  if (env.EKS_HARNESS_URL) return env.EKS_HARNESS_URL.replace(/\/+$/, '')
  if (cachedBase) return cachedBase
  const configFile = path.join(configDir(env), 'config.json')
  cachedBase = fs.existsSync(configFile) ? localUrl(readConfig(env)) : (urlFromCli() ?? localUrl(readConfig(env)))
  return cachedBase.replace(/\/+$/, '')
}

export function harnessBinary(env = process.env) {
  if (env.EKS_HARNESS_BIN && fs.existsSync(env.EKS_HARNESS_BIN)) return env.EKS_HARNESS_BIN
  const names = process.platform === 'win32' ? ['eks-harness.exe', 'eks-harness.cmd', 'eks-harness'] : ['eks-harness']
  for (const dir of (env.PATH || '').split(path.delimiter)) {
    for (const name of names) {
      const candidate = path.join(dir, name)
      if (dir && fs.existsSync(candidate)) return candidate
    }
  }
  const local = path.join(os.homedir(), '.local', 'bin', 'eks-harness')
  return fs.existsSync(local) ? local : null
}

export function apiKey(env = process.env) {
  if (env.EKS_HARNESS_API_KEY && env.EKS_HARNESS_API_KEY.trim()) return env.EKS_HARNESS_API_KEY.trim()
  const stored = readJson(path.join(configDir(env), 'credentials.json'))
  return stored?.apiKey ?? null
}

function sameOrigin(target, base) {
  try {
    const a = new URL(target)
    const b = new URL(base)
    return a.protocol === b.protocol && a.host === b.host
  } catch {
    return false
  }
}

function headersFor(url, extra = {}) {
  const headers = { 'User-Agent': USER_AGENT, Accept: 'application/json', ...extra }
  const key = apiKey()
  if (key && sameOrigin(url, baseUrl())) headers.Authorization = `Bearer ${key}`
  return headers
}

async function parseError(response, route) {
  const text = await response.text().catch(() => '')
  let payload
  try {
    payload = JSON.parse(text)
  } catch {
    payload = { error: 'http_error', message: text.slice(0, 500) || `HTTP ${response.status}` }
  }
  if (response.status === 410) return new LeaseReleasedError(response.status, payload, route)
  return new HarnessApiError(response.status, payload, route)
}

export async function request(method, route, { json, form, timeout = 60_000, query } = {}) {
  const base = baseUrl()
  const url = new URL(base + route)
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value !== undefined && value !== null && value !== '') url.searchParams.set(key, String(value))
  }
  const init = { method, headers: headersFor(url.href), signal: AbortSignal.timeout(timeout) }
  if (json !== undefined) {
    init.headers['Content-Type'] = 'application/json'
    init.body = JSON.stringify(json)
  } else if (form !== undefined) {
    init.body = form
  }
  let response
  try {
    response = await fetch(url, init)
  } catch (error) {
    throw new DaemonUnavailableError(`the harness daemon is not reachable at ${base} (${error.cause?.code ?? error.message}); start it with: eks-harness daemon start`)
  }
  if (!response.ok) throw await parseError(response, route)
  if (response.status === 204) return {}
  const type = response.headers.get('content-type') || ''
  return type.includes('application/json') ? response.json() : response.text()
}

export function readSids(stateDir) {
  const found = {}
  if (!stateDir) return found
  for (const name of SID_FILES) {
    try {
      const value = fs.readFileSync(path.join(stateDir, name), 'utf8').trim()
      if (value) found[name] = value
    } catch {}
  }
  return found
}

function stateDirFromEnv() {
  return process.env.STATE_DIR || process.env.HARNESS_STATE_DIR || null
}

const released = new Set()
let lastBeat = 0

export function leaseReleased(sid) {
  return released.has(sid)
}

export async function heartbeatSids(sids) {
  const results = {}
  for (const sid of sids) {
    try {
      results[sid] = await request('POST', `/api/leases/${encodeURIComponent(sid)}/heartbeat`, { json: {}, timeout: 10_000 })
      released.delete(sid)
    } catch (error) {
      if (error instanceof LeaseReleasedError || error.status === 404) released.add(sid)
      results[sid] = { error: error.message }
    }
  }
  return results
}

export function heartbeat({ force = false, stateDir = stateDirFromEnv(), sid = process.env.EKS_LEASE_SID } = {}) {
  const now = Date.now()
  if (!force && now - lastBeat < 20_000) return
  lastBeat = now
  const sids = new Set(Object.values(readSids(stateDir)))
  if (sid) sids.add(sid)
  if (sids.size) heartbeatSids([...sids]).catch(() => {})
}

export async function updateLeaseMeta(sid, meta) {
  return request('POST', `/api/leases/${encodeURIComponent(sid)}/meta`, { json: { meta }, timeout: 15_000 })
}

export async function sidInfo(sid) {
  return request('GET', `/api/sid/${encodeURIComponent(sid)}`, { timeout: 15_000 })
}

export async function leaseInfo(sid) {
  return request('GET', `/api/leases/${encodeURIComponent(sid)}`, { timeout: 15_000 })
}

export async function chromeEndpoint(sid = process.env.EKS_LEASE_SID || readSids(stateDirFromEnv()).sid) {
  if (!sid) throw new Error('this web driver holds no browser lease sid')
  const browser = await request('POST', `/api/leases/${encodeURIComponent(sid)}/browser`, { json: {}, timeout: 90_000 })
  if (!browser.cdp) throw new Error(`the browser lease ${sid} has no CDP endpoint`)
  return browser.cdp
}

const MIME_BY_EXT = {
  '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.mp4': 'video/mp4', '.webm': 'video/webm',
  '.html': 'text/html', '.mhtml': 'multipart/related', '.har': 'application/json', '.json': 'application/json',
  '.txt': 'text/plain', '.log': 'text/plain', '.yaml': 'text/plain', '.zip': 'application/zip', '.pdf': 'application/pdf',
}

export function mimeFor(file) {
  return MIME_BY_EXT[path.extname(file).toLowerCase()] ?? 'application/octet-stream'
}

async function fileBlob(file, mime) {
  if (typeof fs.openAsBlob === 'function') return fs.openAsBlob(file, { type: mime })
  return new Blob([fs.readFileSync(file)], { type: mime })
}

export async function uploadArtifact({ file, sid, project, session, kind, caption, tags, meta, filename, mime, source = 'agent' }) {
  if (!file || !fs.existsSync(file)) throw new Error(`nothing to upload: ${file}`)
  if (!sid && !project) throw new Error('an upload needs a sid (--sid) or a project')
  const name = filename || path.basename(file)
  const form = new FormData()
  if (sid) form.append('sid', sid)
  if (project) form.append('project', project)
  if (session) form.append('session', session)
  if (kind) form.append('kind', kind)
  if (caption) form.append('caption', caption)
  const tagList = Array.isArray(tags) ? tags : String(tags ?? '').split(',')
  const cleanTags = tagList.map((tag) => String(tag).trim()).filter(Boolean)
  if (cleanTags.length) form.append('tags', cleanTags.join(','))
  if (meta && Object.keys(meta).length) form.append('meta', JSON.stringify(meta))
  if (source) form.append('source', source)
  form.append('filename', name)
  form.append('file', await fileBlob(file, mime || mimeFor(name)), name)
  return request('POST', '/api/artifacts', { form, timeout: 15 * 60_000 })
}

export async function uploadAndDelete(options) {
  const result = await uploadArtifact(options)
  fs.rmSync(options.file, { force: true })
  return result
}

export async function captureDevice(sid, action, body = {}) {
  const route = {
    screenshot: 'screenshot', 'video-start': 'video/start', 'video-stop': 'video/stop', 'video-reset': 'video/reset',
  }[action]
  if (!route) throw new Error(`unknown capture action ${action}`)
  return request('POST', `/api/captures/${encodeURIComponent(sid)}/${route}`, { json: body, timeout: 10 * 60_000 })
}

export async function getArtifact(id) {
  return request('GET', `/api/artifacts/${encodeURIComponent(id)}`)
}

export async function downloadArtifact(id, destDir) {
  const artifact = await getArtifact(id)
  const raw = new URL(artifact.rawUrl ?? artifact.raw_url)
  const url = new URL(baseUrl() + raw.pathname)
  url.searchParams.set('download', '1')
  let response
  try {
    response = await fetch(url, { headers: headersFor(url.href, { Accept: '*/*' }), signal: AbortSignal.timeout(15 * 60_000) })
  } catch (error) {
    throw new DaemonUnavailableError(`the harness daemon is not reachable at ${baseUrl()} (${error.message})`)
  }
  if (!response.ok) throw await parseError(response, url.pathname)
  fs.mkdirSync(destDir, { recursive: true })
  const target = path.join(destDir, `${artifact.id}-${artifact.filename}`)
  const temp = `${target}.part`
  fs.writeFileSync(temp, Buffer.from(await response.arrayBuffer()))
  fs.renameSync(temp, target)
  return { path: target, artifact }
}

export async function listArtifacts({ project, session, kinds = [] } = {}) {
  const items = []
  let cursor
  do {
    const page = await request('GET', '/api/artifacts', { query: { project, session, cursor }, timeout: 60_000 })
    for (const item of page.items ?? []) {
      if (!kinds.length || kinds.includes(item.kind)) items.push(item)
    }
    cursor = page.nextCursor ?? null
  } while (cursor)
  return items
}

export async function deviceLog(resource, lines = 400) {
  return request('GET', '/api/logs', { query: { resource, lines }, timeout: 30_000 })
}

export function tempCaptureDir() {
  const dir = path.join(os.tmpdir(), 'eks-harness-captures')
  fs.mkdirSync(dir, { recursive: true })
  return dir
}

export function tempCaptureFile(name, extension) {
  const safe = String(name || 'capture').replace(/[^a-zA-Z0-9._-]/g, '-').slice(0, 80)
  const time = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19)
  return path.join(tempCaptureDir(), `${time}-${process.pid}-${Math.random().toString(36).slice(2, 8)}-${safe}.${extension}`)
}

export function linksOf(artifact) {
  return {
    id: artifact.id,
    kind: artifact.kind,
    size: artifact.size,
    caption: artifact.caption ?? '',
    url: artifact.url,
    rawUrl: artifact.rawUrl,
    downloadUrl: artifact.downloadUrl,
    sessionUrl: artifact.sessionUrl,
  }
}

function humanSize(bytes) {
  if (!Number.isFinite(bytes)) return ''
  const units = ['B', 'KB', 'MB', 'GB']
  let value = bytes
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit++
  }
  return `${value.toFixed(unit ? 1 : 0)} ${units[unit]}`
}

export function formatLinks(artifact) {
  return [
    `${artifact.kind} ${artifact.id} (${humanSize(artifact.size)})${artifact.caption ? `: ${artifact.caption}` : ''}`,
    `  direct   ${artifact.rawUrl}`,
    `  ui       ${artifact.url}`,
    `  session  ${artifact.sessionUrl}`,
  ].join('\n')
}

