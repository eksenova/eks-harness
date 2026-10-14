import { OpError } from './ops.mjs'

export const MOBILE_ROLE_KINDS = {
  button: 'button',
  link: 'button',
  textbox: 'input',
  searchbox: 'input',
  switch: 'switch',
  checkbox: 'switch',
  heading: 'text',
  text: 'text',
}

export function describeTarget(target) {
  if (!target) return '(none)'
  const args = target.args ?? {}
  const head = target.by === 'test_id' ? `#${args.id}` : `${target.by}=${Object.values(args).filter((v) => typeof v !== 'boolean').join(':')}`
  const parts = [head]
  if (target.within) parts.push(`within ${describeTarget(target.within)}`)
  if (target.near) parts.push(`near ${describeTarget(target.near)}`)
  if (target.nth !== undefined && target.nth !== null) parts.push(`nth ${target.nth}`)
  if (target.surface === 'native') parts.push('(native)')
  return parts.join(' ')
}

function unsupported(target, platform) {
  return new OpError('target_unsupported', `target ${describeTarget(target)} is not supported on ${platform}`, { detail: { target } })
}

export function toWebSpec(target) {
  if (!target) return null
  const args = target.args ?? {}
  let spec
  switch (target.by) {
    case 'test_id':
      spec = { testId: String(args.id) }
      break
    case 'text':
      spec = { text: String(args.text), exact: args.exact !== false }
      break
    case 'role':
      spec = { role: String(args.role), name: args.name ?? undefined, exact: Boolean(args.exact) }
      break
    case 'label':
      spec = { label: String(args.text), exact: Boolean(args.exact) }
      break
    case 'placeholder':
      spec = { placeholder: String(args.text), exact: Boolean(args.exact) }
      break
    case 'css':
      spec = { selector: String(args.selector) }
      break
    case 'point':
      spec = { point: { x: Number(args.x), y: Number(args.y) } }
      break
    case 'rect':
      spec = { point: { x: Number(args.x) + Number(args.w) / 2, y: Number(args.y) + Number(args.h) / 2 } }
      break
    default:
      throw unsupported(target, 'web')
  }
  if (target.within) spec.within = toWebSpec(target.within)
  if (target.near) spec.near = toWebSpec(target.near)
  if (target.nth !== undefined && target.nth !== null) spec.nth = target.nth
  return spec
}

export function toMobileSpec(target, platform = 'mobile') {
  if (!target) return null
  const args = target.args ?? {}
  let spec
  switch (target.by) {
    case 'test_id':
      spec = { testID: String(args.id) }
      break
    case 'text':
      spec = { text: String(args.text), exact: args.exact !== false }
      break
    case 'label':
      spec = { label: String(args.text), exact: Boolean(args.exact) }
      break
    case 'placeholder':
      spec = { placeholder: String(args.text), exact: Boolean(args.exact) }
      break
    case 'kind':
      spec = { kind: String(args.kind) }
      break
    case 'role': {
      const kind = MOBILE_ROLE_KINDS[String(args.role)]
      if (!kind) throw unsupported(target, platform)
      spec = { kind, ...(args.name ? { label: String(args.name), exact: Boolean(args.exact) } : {}) }
      break
    }
    case 'point':
      spec = { point: { x: Number(args.x), y: Number(args.y) } }
      break
    case 'rect':
      spec = { point: { x: Number(args.x) + Number(args.w) / 2, y: Number(args.y) + Number(args.h) / 2 } }
      break
    default:
      throw unsupported(target, platform)
  }
  if (target.within) spec.within = toMobileSpec(target.within, platform)
  if (target.near) spec.near = toMobileSpec(target.near, platform)
  if (target.nth !== undefined && target.nth !== null) spec.nth = target.nth
  return spec
}

export function strictPick(count, target, candidates = []) {
  if (count === 0) {
    throw new OpError('target_not_found', `no element matches ${describeTarget(target)}`, { detail: { target, nearMisses: candidates }, retryable: true })
  }
  const nth = target?.nth
  if (nth === undefined || nth === null) {
    if (count > 1) {
      throw new OpError('target_ambiguous', `${count} elements match ${describeTarget(target)}; add nth, first or last, or narrow it with within`, { detail: { target, count, candidates: candidates.slice(0, 10) } })
    }
    return 0
  }
  const index = nth < 0 ? count + nth : nth
  if (index < 0 || index >= count) {
    throw new OpError('target_not_found', `${describeTarget(target)} asks for match ${nth} but ${count} match`, { detail: { target, count }, retryable: true })
  }
  return index
}
