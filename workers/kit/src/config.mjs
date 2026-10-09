import fs from 'node:fs'

export const DEFAULT_THEME = {
  surface: '#141518',
  surfaceDeep: '#0A0A0B',
  surfaceRgb: '20,21,24',
  onSurface: '#FFFFFF',
  accent: '#FF6247',
  accentLight: '#FF8A73',
  accentRgb: '255,98,71',
  pointerStroke: '#141518',
  font: 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif',
}

export function loadWorkerConfig(env = process.env) {
  const raw = env.EHX_WORKER_CONFIG
  let config = {}
  if (raw) {
    const text = raw.trim().startsWith('{') ? raw : fs.readFileSync(raw, 'utf8')
    config = JSON.parse(text)
  }
  if (env.EHX_WORKER_PORT && config.port === undefined) config.port = Number(env.EHX_WORKER_PORT)
  return config
}

function hexRgb(hex) {
  const match = /^#?([0-9a-f]{6})$/i.exec(String(hex ?? '').trim())
  if (!match) return null
  const value = parseInt(match[1], 16)
  return `${(value >> 16) & 255},${(value >> 8) & 255},${value & 255}`
}

export function resolveTheme(theme = {}, locale = 'en-US') {
  const merged = { ...DEFAULT_THEME, ...(theme ?? {}), locale }
  if (theme?.accent && !theme.accentRgb) merged.accentRgb = hexRgb(theme.accent) ?? merged.accentRgb
  if (theme?.accent && !theme.accentLight) merged.accentLight = theme.accent
  if (theme?.surface && !theme.surfaceRgb) merged.surfaceRgb = hexRgb(theme.surface) ?? merged.surfaceRgb
  if (theme?.surface && !theme.surfaceDeep) merged.surfaceDeep = theme.surface
  return merged
}
