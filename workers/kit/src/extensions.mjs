import { pathToFileURL } from 'node:url'

const HOOKS = ['helpers', 'routes', 'onContext', 'onPage', 'onRestore', 'onRecordingStart', 'status', 'apiHeaders', 'onBridge', 'onEvent']

export async function loadExtensions(entries, kit, log = () => {}) {
  const loaded = []
  for (const entry of entries ?? []) {
    const spec = typeof entry === 'string' ? { module: entry } : entry
    const id = spec.id || spec.module
    try {
      const mod = await import(pathToFileURL(spec.module).href)
      const factory = mod.default ?? mod.extend
      if (typeof factory !== 'function') throw new Error('the module has no default export function extend(kit)')
      const result = (await factory({ ...kit, id, settings: spec.settings ?? {} })) ?? {}
      const ext = { id }
      for (const hook of HOOKS) if (result[hook] !== undefined) ext[hook] = result[hook]
      loaded.push(ext)
      log(`extension ${id} loaded`)
    } catch (error) {
      log(`extension ${id} failed to load: ${error?.stack ?? error}`)
      if (spec.required !== false) throw new Error(`driver extension ${id} failed to load: ${error.message}`)
    }
  }
  return loaded
}
