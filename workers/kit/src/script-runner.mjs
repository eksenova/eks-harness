const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor

class AbortedRun extends Error {
  constructor() {
    super('run aborted')
    this.name = 'AbortedRun'
  }
}

const SECRET_KEY = /pass(word)?|secret|token|otp|pin/i

function summarize(value, depth = 0) {
  if (value === undefined) return undefined
  if (value === null || typeof value === 'number' || typeof value === 'boolean') return value
  if (typeof value === 'string') return value.length > 120 ? `${value.slice(0, 117)}...` : value
  if (typeof value === 'function') return value.name ? `fn ${value.name}` : 'fn'
  if (Array.isArray(value)) {
    if (depth > 1) return `[${value.length} items]`
    const items = value.slice(0, 8).map((item) => summarize(item, depth + 1))
    return value.length > 8 ? [...items, `+${value.length - 8} more`] : items
  }
  if (typeof value === 'object') {
    if (depth > 1) return '{...}'
    const out = {}
    for (const [key, inner] of Object.entries(value).slice(0, 12)) {
      out[key] = SECRET_KEY.test(key) && typeof inner === 'string' ? '********' : summarize(inner, depth + 1)
    }
    return out
  }
  return String(value)
}

function isArtifactLink(value) {
  return typeof value === 'object' && value !== null && typeof value.id === 'string' && typeof value.rawUrl === 'string' && typeof value.url === 'string'
}

function artifactLine(link) {
  return `${link.kind ?? 'artifact'} ${link.id}${link.caption ? ` "${link.caption}"` : ''}\n    direct  ${link.rawUrl}\n    ui      ${link.url}`
}

function collectArtifacts(value, into, depth = 0) {
  if (depth > 4 || value === null || value === undefined) return
  if (isArtifactLink(value)) {
    const line = artifactLine(value)
    if (!into.includes(line)) into.push(line)
    return
  }
  if (Array.isArray(value)) {
    for (const item of value) collectArtifacts(item, into, depth + 1)
    return
  }
  if (typeof value === 'object') {
    for (const inner of Object.values(value)) collectArtifacts(inner, into, depth + 1)
  }
}

function errorText(error) {
  if (!error) return 'unknown error'
  const message = error.message ?? String(error)
  return message.length > 1500 ? `${message.slice(0, 1500)}...` : message
}

export function createRunner({ helpers, snapshot, onStep, log = () => {}, idPrefix = '' }) {
  let run = null
  let counter = 0

  const elapsed = () => (run ? (run.endedAt ?? Date.now()) - run.startedAt : 0)

  function summary({ full = false } = {}) {
    if (!run) return { status: 'idle' }
    const steps = full ? run.steps : run.steps.slice(-40)
    return {
      id: run.id,
      status: run.status,
      elapsedMs: elapsed(),
      stepCount: run.steps.length,
      steps,
      omittedSteps: run.steps.length - steps.length,
      logs: run.logs.slice(-60),
      artifacts: run.artifacts,
      pause: run.pause,
      value: run.value,
      error: run.error,
      snapshot: run.finalSnapshot,
    }
  }

  function report() {
    const waiters = run.waiters.splice(0)
    const payload = summary()
    for (const resolve of waiters) resolve(payload)
  }

  function waitForReport(waitMs) {
    if (!run) return Promise.resolve({ status: 'idle' })
    if (run.status !== 'running') return Promise.resolve(summary())
    return new Promise((resolve) => {
      run.waiters.push(resolve)
      const timer = setTimeout(() => {
        const index = run?.waiters.indexOf(resolve) ?? -1
        if (index >= 0) run.waiters.splice(index, 1)
        resolve({ ...summary(), note: `still running after ${waitMs}ms - call wait to keep waiting` })
      }, waitMs)
      timer.unref?.()
    })
  }

  async function pause(current, info) {
    current.status = 'paused'
    current.pause = { ...info, atMs: Date.now() - current.startedAt }
    try {
      current.pause.snapshot = await snapshot({ reason: info.reason, shot: info.shot, label: info.label })
      collectArtifacts(current.pause.snapshot?.shots, current.artifacts)
    } catch (error) {
      current.pause.snapshot = { error: `snapshot failed: ${errorText(error)}` }
    }
    report()
    const action = await new Promise((resolve) => {
      current.resume = resolve
    })
    current.resume = null
    current.pause = null
    current.status = 'running'
    if (action === 'abort') throw new AbortedRun()
    return action
  }

  function wrap(current, name, fn, { pausing }) {
    return async (...args) => {
      if (current.aborted) throw new AbortedRun()
      const index = current.steps.length + 1
      if (pausing && current.breakBefore.some((mark) => mark === index || mark === name)) {
        await pause(current, { reason: 'break-before', step: { i: index, name, args: summarize(args) } })
      }
      for (;;) {
        const entry = { i: current.steps.length + 1, name, args: summarize(args), atMs: Date.now() - current.startedAt }
        const started = Date.now()
        try {
          const value = await fn(...args)
          collectArtifacts(value, current.artifacts)
          entry.ms = Date.now() - started
          entry.ok = true
          if (value !== undefined && value !== null && typeof value !== 'object') entry.value = summarize(value)
          current.steps.push(entry)
          onStep?.(entry)
          return value
        } catch (error) {
          if (error instanceof AbortedRun) throw error
          entry.ms = Date.now() - started
          entry.ok = false
          entry.error = errorText(error)
          current.steps.push(entry)
          onStep?.(entry)
          if (!pausing || !current.pauseOnError) throw error
          const action = await pause(current, { reason: 'error', step: entry, error: entry.error })
          if (action === 'retry') continue
          if (action === 'skip') return undefined
          throw error
        }
      }
    }
  }

  function bind(current, { pausing }) {
    const scope = {}
    for (const [name, value] of Object.entries(helpers())) {
      if (typeof value === 'function' && !value.raw) {
        scope[name] = wrap(current, name, value, { pausing })
      } else if (value && typeof value === 'object' && value.grouped) {
        const group = {}
        for (const [inner, fn] of Object.entries(value)) {
          if (typeof fn === 'function') group[inner] = wrap(current, `${name}.${inner}`, fn, { pausing })
        }
        scope[name] = group
      } else {
        scope[name] = value
      }
    }
    scope.log = (...parts) => {
      const line = parts.map((part) => (typeof part === 'string' ? part : JSON.stringify(summarize(part)))).join(' ')
      current.logs.push({ atMs: Date.now() - current.startedAt, line: line.slice(0, 2000) })
    }
    scope.sleep = wrap(current, 'sleep', (ms) => new Promise((resolve) => setTimeout(resolve, ms)), { pausing })
    scope.assert = wrap(current, 'assert', (condition, message) => {
      if (!condition) throw new Error(`assertion failed: ${message ?? 'condition was falsy'}`)
      return true
    }, { pausing })
    scope.breakpoint = async (label = 'breakpoint', options = {}) => {
      if (!pausing) {
        scope.log(`(breakpoint "${label}" ignored inside exec)`)
        return
      }
      if (current.aborted) throw new AbortedRun()
      current.steps.push({ i: current.steps.length + 1, name: 'breakpoint', args: label, atMs: Date.now() - current.startedAt, ok: true, ms: 0 })
      await pause(current, { reason: 'breakpoint', label, shot: options.shot })
    }
    return scope
  }

  function compile(source, scope) {
    const names = Object.keys(scope)
    const fn = new AsyncFunction(...names, `"use strict";\n${source}`)
    return () => fn(...names.map((name) => scope[name]))
  }

  async function start(source, options = {}) {
    let replaced
    if (run && (run.status === 'running' || run.status === 'paused')) {
      if (run.status === 'running' && !options.force) {
        throw new Error(`run ${run.id} is still running - wait for it, abort it, or pass force`)
      }
      replaced = `aborted the ${run.status} run ${run.id} to start this one`
      abort()
    }
    const current = {
      id: `${idPrefix}r${++counter}`,
      status: 'running',
      startedAt: Date.now(),
      steps: [],
      logs: [],
      artifacts: [],
      waiters: [],
      breakBefore: (options.breakBefore ?? []).map((mark) => (/^\d+$/.test(String(mark)) ? Number(mark) : String(mark))),
      pauseOnError: options.pauseOnError !== false,
      finalSnapshot: undefined,
    }
    if (replaced) current.logs.push({ atMs: 0, line: replaced })
    run = current
    const scope = bind(current, { pausing: true })
    let body
    try {
      body = compile(source, scope)
    } catch (error) {
      current.status = 'failed'
      current.error = `script does not compile: ${errorText(error)}`
      return summary()
    }
    const finish = async (status, value, error) => {
      if (current.status === 'aborted') return
      current.endedAt = Date.now()
      current.status = status
      current.value = summarize(value)
      current.error = error ? errorText(error) : undefined
      if (options.snapshot !== false) {
        try {
          current.finalSnapshot = await snapshot({ reason: status, shot: options.shotAtEnd })
          collectArtifacts(current.finalSnapshot?.shots, current.artifacts)
        } catch (snapshotError) {
          current.finalSnapshot = { error: `snapshot failed: ${errorText(snapshotError)}` }
        }
      }
      log(`run ${current.id} ${status} in ${Date.now() - current.startedAt}ms, ${current.steps.length} steps`)
      if (run === current) report()
    }
    Promise.resolve()
      .then(body)
      .then((value) => finish('done', value), (error) => finish(error instanceof AbortedRun ? 'aborted' : 'failed', undefined, error))
    return waitForReport(options.waitMs ?? 100_000)
  }

  function resume(action = 'continue', options = {}) {
    if (!run) throw new Error('no run to continue')
    if (run.status !== 'paused') throw new Error(`run ${run.id} is ${run.status}, not paused`)
    if (!['continue', 'retry', 'skip', 'abort'].includes(action)) throw new Error(`unknown action ${action}`)
    if (options.breakBefore) {
      run.breakBefore = options.breakBefore.map((mark) => (/^\d+$/.test(String(mark)) ? Number(mark) : String(mark)))
    }
    const current = run
    current.status = 'running'
    current.pause = null
    current.resume(action)
    if (action === 'abort') return Promise.resolve({ ...summary(), status: 'aborting' })
    return waitForReport(options.waitMs ?? 100_000)
  }

  function abort() {
    if (!run) return { status: 'idle' }
    const current = run
    current.aborted = true
    if (current.status === 'paused') current.resume?.('abort')
    if (current.status === 'running' || current.status === 'paused') current.status = 'aborted'
    current.pause = null
    report()
    return summary()
  }

  async function exec(source) {
    const current = {
      id: 'exec',
      status: 'running',
      startedAt: Date.now(),
      steps: [],
      logs: [],
      artifacts: [],
      waiters: [],
      breakBefore: [],
      pauseOnError: false,
    }
    const scope = bind(current, { pausing: false })
    try {
      const value = await compile(source, scope)()
      collectArtifacts(value, current.artifacts)
      return { ok: true, value: summarize(value), raw: value, steps: current.steps, logs: current.logs, artifacts: current.artifacts, elapsedMs: Date.now() - current.startedAt }
    } catch (error) {
      return { ok: false, error: errorText(error), steps: current.steps, logs: current.logs, artifacts: current.artifacts, elapsedMs: Date.now() - current.startedAt }
    }
  }

  return {
    start,
    resume,
    abort,
    exec,
    wait: (waitMs = 100_000) => waitForReport(waitMs),
    status: (options) => summary(options),
  }
}

function formatStep(step) {
  const mark = step.ok ? 'ok  ' : 'FAIL'
  const args = step.args === undefined ? '' : ` ${typeof step.args === 'string' ? JSON.stringify(step.args) : JSON.stringify(step.args)}`
  const value = step.value !== undefined ? ` -> ${JSON.stringify(step.value)}` : ''
  const error = step.error ? `\n         ! ${step.error.split('\n').join('\n           ')}` : ''
  return `  ${String(step.i).padStart(3)} ${mark} ${String(step.ms ?? 0).padStart(6)}ms  ${step.name}${args.length > 220 ? `${args.slice(0, 217)}...` : args}${value}${error}`
}

function formatSnapshot(snap) {
  if (!snap) return []
  if (snap.error) return ['snapshot:', `  ${snap.error}`]
  const lines = ['snapshot:']
  for (const [key, value] of Object.entries(snap)) {
    if (key === 'tree') continue
    if (value === undefined || value === null || (Array.isArray(value) && value.length === 0)) continue
    if (Array.isArray(value)) {
      lines.push(`  ${key}:`)
      for (const item of value) lines.push(`    - ${typeof item === 'string' ? item : JSON.stringify(item)}`)
    } else if (typeof value === 'object') {
      lines.push(`  ${key}: ${JSON.stringify(value)}`)
    } else {
      lines.push(`  ${key}: ${value}`)
    }
  }
  if (snap.tree) {
    lines.push('  tree:')
    for (const line of String(snap.tree).split('\n')) lines.push(`    ${line}`)
  }
  return lines
}

export function renderText(result) {
  if (!result || result.status === 'idle') return 'no run (idle)\n'
  if (result.id === 'exec' || result.raw !== undefined || (result.ok !== undefined && !result.status)) {
    const lines = [result.ok ? `exec ok in ${result.elapsedMs}ms` : `exec FAILED in ${result.elapsedMs}ms: ${result.error}`]
    for (const step of result.steps ?? []) lines.push(formatStep(step))
    for (const entry of result.logs ?? []) lines.push(`  log ${entry.line}`)
    if (result.artifacts?.length) lines.push('artifacts:', ...result.artifacts.map((file) => `  ${file}`))
    if (result.ok && result.value === undefined && typeof result.raw === 'string') {
      lines.length = 0
      lines.push(result.raw)
    } else if (result.ok && result.value !== undefined) {
      lines.push('value:')
      const text = typeof result.raw === 'string' ? result.raw : JSON.stringify(result.value, null, 2)
      for (const line of String(text).split('\n')) lines.push(`  ${line}`)
    }
    return `${lines.join('\n')}\n`
  }
  const seconds = (result.elapsedMs / 1000).toFixed(2)
  let head = `run ${result.id} ${result.status.toUpperCase()} after ${seconds}s, ${result.stepCount} steps`
  if (result.pause) {
    const where = result.pause.reason === 'breakpoint' ? `breakpoint "${result.pause.label}"`
      : result.pause.reason === 'error' ? `error in step ${result.pause.step?.i} (${result.pause.step?.name})`
        : `break before step ${result.pause.step?.i} (${result.pause.step?.name})`
    head = `run ${result.id} PAUSED at ${where} after ${seconds}s`
  }
  const lines = [head]
  if (result.note) lines.push(`(${result.note})`)
  if (result.omittedSteps) lines.push(`  ... ${result.omittedSteps} earlier steps omitted`)
  for (const step of result.steps ?? []) lines.push(formatStep(step))
  if (result.logs?.length) {
    lines.push('logs:')
    for (const entry of result.logs) lines.push(`  [${entry.atMs}ms] ${entry.line}`)
  }
  if (result.artifacts?.length) lines.push('artifacts:', ...result.artifacts.map((file) => `  ${file}`))
  if (result.error) lines.push(`error: ${result.error}`)
  if (result.value !== undefined) lines.push(`value: ${JSON.stringify(result.value)}`)
  lines.push(...formatSnapshot(result.pause?.snapshot ?? result.snapshot))
  if (result.status === 'paused') {
    const options = result.pause?.reason === 'error'
      ? 'continue retry | continue skip | continue (rethrow) | abort'
      : 'continue | abort'
    lines.push(`next: exec '<js>' / shot / tree to inspect, then ${options}`)
  }
  return `${lines.join('\n')}\n`
}
