import fs from 'node:fs'
import path from 'node:path'

import { validate } from './schema.mjs'

export const PHASES = ['run', 'enter', 'exit']
export const FILE_ROLES = ['value', 'shot', 'evidence']

export class OpError extends Error {
  constructor(code, message, { detail = {}, retryable = false } = {}) {
    super(message)
    this.code = code
    this.detail = detail
    this.retryable = retryable
  }
}

export function loadSchemas(file) {
  if (!file) return { ops: {}, $defs: {} }
  return JSON.parse(fs.readFileSync(file, 'utf8'))
}

export function createOpRegistry({ platform, schemas = { ops: {}, $defs: {} }, makeContext = (ctx) => ctx, log = () => {} } = {}) {
  const ops = new Map()

  function define(definition) {
    const { name } = definition
    if (!name || typeof name !== 'string') throw new Error('defineOp needs a name such as ui.tap')
    if (!definition.run && !definition.enter) throw new Error(`op ${name} needs run, or enter and exit`)
    if (definition.enter && !definition.exit) throw new Error(`container op ${name} needs exit with enter`)
    const platforms = definition.platforms ?? null
    if (platforms && !platforms.includes(platform)) return false
    ops.set(name, definition)
    return true
  }

  function names() {
    return [...ops.keys()].sort()
  }

  function check(name, args) {
    const schema = schemas.ops?.[name]
    if (!schema) return
    const errors = validate(schema, args ?? {}, schemas.$defs ?? {})
    if (errors.length) {
      throw new OpError('args_invalid', `${name}: ${errors.slice(0, 5).join('; ')}`, { detail: { errors } })
    }
  }

  async function handle(body) {
    const { op, phase = 'run', args = {}, site = {}, session = {}, resources = {}, deadlineMs = null, outDir = null } = body ?? {}
    const files = []
    try {
      if (!PHASES.includes(phase)) throw new OpError('args_invalid', `unknown phase ${phase}`)
      const definition = ops.get(op)
      if (!definition) {
        throw new OpError('op_unsupported', `${op} is not supported on ${platform} by this worker`, { detail: { op, platform } })
      }
      const handler = phase === 'run' ? definition.run : definition[phase]
      if (!handler) throw new OpError('op_unsupported', `${op} has no ${phase} phase`, { detail: { op, phase } })
      check(op, args)
      if (outDir) fs.mkdirSync(outDir, { recursive: true })
      const base = {
        op,
        phase,
        site,
        session,
        resources,
        platform,
        deadline: deadlineMs,
        outDir,
        remaining: () => (deadlineMs ? Math.max(0, deadlineMs - Date.now()) : null),
        file: (name) => {
          if (!outDir) throw new OpError('driver_error', `${op} writes files but the call has no outDir`)
          return path.join(outDir, name)
        },
        addFile: (file) => {
          if (!FILE_ROLES.includes(file.role ?? 'evidence')) throw new Error(`unknown file role ${file.role}`)
          files.push({ role: 'evidence', caption: '', ...file })
          return file.path
        },
        log,
      }
      const ctx = makeContext(base)
      const work = Promise.resolve(handler(args, ctx))
      const value = await withDeadline(work, deadlineMs, op)
      return { ok: true, value: value === undefined ? null : value, files, detail: ctx.detail ?? {} }
    } catch (error) {
      return { ok: false, error: toError(error), files }
    }
  }

  return { define, names, handle, get: (name) => ops.get(name) }
}

function withDeadline(work, deadlineMs, op) {
  if (!deadlineMs) return work
  const left = deadlineMs - Date.now()
  if (left <= 0) return Promise.reject(new OpError('timeout', `${op} had no time left`, { retryable: true }))
  let timer
  const expired = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new OpError('timeout', `${op} did not finish within ${left} ms`, { retryable: true })), left)
  })
  return Promise.race([work, expired]).finally(() => clearTimeout(timer))
}

export function toError(error) {
  if (error instanceof OpError) {
    return { code: error.code, message: error.message, detail: error.detail ?? {}, retryable: Boolean(error.retryable) }
  }
  const message = error?.message ?? String(error)
  if (error?.name === 'TimeoutError' || /Timeout \d+ms exceeded/.test(message)) {
    return { code: 'timeout', message, detail: {}, retryable: true }
  }
  return { code: 'driver_error', message, detail: { stack: String(error?.stack ?? '').split('\n').slice(0, 6).join('\n') }, retryable: false }
}
