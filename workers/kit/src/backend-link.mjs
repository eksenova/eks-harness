function keyOf(value) {
  return value && value.id ? `${value.id}#${value.generation ?? 0}` : null
}

export function backendFromLink(data) {
  if (!data || !data.backend) return null
  return {
    ...(data.info ?? {}),
    id: data.backend.id,
    lease: data.lease?.id ?? null,
    generation: data.backend.generation ?? 0,
    status: data.backend.status ?? null,
  }
}

export function createBackendLink({ initial = null, fetchInfo, onChange = async () => {}, log = () => {} } = {}) {
  let current = initial && initial.id ? { ...initial } : null
  let pending = null

  async function observe(summary) {
    if (pending) return pending
    const known = keyOf(current)
    if (!summary) {
      if (!current) return false
      const previous = current
      current = null
      pending = Promise.resolve(onChange(null, previous)).then(() => true).finally(() => { pending = null })
      return pending
    }
    if (summary.status !== 'running' || keyOf(summary) === known) return false
    pending = (async () => {
      let next = { ...summary, lease: summary.lease ?? null }
      try {
        const fetched = fetchInfo ? backendFromLink(await fetchInfo()) : null
        if (fetched) next = fetched
      } catch (error) {
        log(`backend ${summary.id}: could not read its info: ${error?.message ?? error}`)
      }
      const previous = current
      current = next
      try {
        await onChange(next, previous)
      } catch (error) {
        log(`backend change handler failed: ${error?.message ?? error}`)
      }
      return true
    })().finally(() => { pending = null })
    return pending
  }

  return {
    get: () => current,
    observe,
  }
}
