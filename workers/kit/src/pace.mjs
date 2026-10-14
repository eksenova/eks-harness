const PACE_DEFAULTS = { moveMs: 300, dwellMs: 400, typeMsPerChar: 40, holdMs: 700, screenHoldMs: 1500, mobilePressMs: 250 }
const PACE_KEYS = [
  ['moveMs', 'move_ms', 'move'],
  ['dwellMs', 'dwell_ms', 'dwell'],
  ['typeMsPerChar', 'type_ms_per_char', 'type_per_char'],
  ['holdMs', 'hold_ms', 'hold'],
  ['screenHoldMs', 'screen_hold_ms', 'screen_hold'],
  ['mobilePressMs', 'mobile_press_ms', 'press'],
]

export function createPace(configured = {}) {
  const base = { ...PACE_DEFAULTS, ...(configured ?? {}) }
  const known = new Set(PACE_KEYS.flat())

  function parsePace(spec) {
    if (spec === undefined || spec === null || spec === '' || spec === 'demo') return { name: 'demo', ...base }
    let object
    try {
      object = typeof spec === 'string' ? JSON.parse(spec) : spec
    } catch {
      throw new Error(`pace takes demo or a pace object (got ${JSON.stringify(spec)?.slice(0, 80)})`)
    }
    if (!object || typeof object !== 'object' || Array.isArray(object)) throw new Error('pace takes demo or a pace object')
    for (const key of Object.keys(object)) {
      if (!known.has(key)) throw new Error(`unknown pace key ${JSON.stringify(key)}`)
    }
    const merged = { ...base }
    for (const names of PACE_KEYS) {
      const value = names.map((name) => object[name]).find((item) => item !== undefined && item !== null)
      if (value === undefined) continue
      if (!Number.isInteger(value) || value < 0) throw new Error(`pace.${names[0]} must be a non-negative integer of ms`)
      merged[names[0]] = value
    }
    return { name: typeof spec === 'string' ? spec : 'custom', ...merged }
  }

  return { base, parsePace }
}
