export const BOXES_SCHEMA = 'eks/boxes@1'
export const RECORDING_SCHEMA = 'eks/recording-boxes@1'
export const MAX_NODES = 3000

export function domBoxMap(doc, win, opts = {}) {
  const max = opts.max ?? 3000
  const full = Boolean(opts.full)
  const root = doc.documentElement
  const offsetX = opts.offsetX ?? (full ? win.scrollX : 0)
  const offsetY = opts.offsetY ?? (full ? win.scrollY : 0)
  const regionW = opts.width ?? (full && root ? root.scrollWidth : win.innerWidth)
  const regionH = opts.height ?? (full && root ? root.scrollHeight : win.innerHeight)
  const IMPLICIT = {
    a: (el) => (el.getAttribute('href') !== null ? 'link' : null),
    button: () => 'button',
    textarea: () => 'textbox',
    select: (el) => (el.getAttribute('multiple') !== null || Number(el.getAttribute('size') || 0) > 1 ? 'listbox' : 'combobox'),
    img: (el) => (el.getAttribute('alt') === '' ? 'presentation' : 'img'),
    h1: () => 'heading', h2: () => 'heading', h3: () => 'heading', h4: () => 'heading', h5: () => 'heading', h6: () => 'heading',
    nav: () => 'navigation', main: () => 'main', aside: () => 'complementary', dialog: () => 'dialog',
    ul: () => 'list', ol: () => 'list', li: () => 'listitem', table: () => 'table', tr: () => 'row',
    td: () => 'cell', th: () => 'columnheader', option: () => 'option', progress: () => 'progressbar',
    article: () => 'article', fieldset: () => 'group', hr: () => 'separator', form: () => 'form',
    header: () => 'banner', footer: () => 'contentinfo', summary: () => 'button', label: () => null,
    input: (el) => {
      const type = String(el.getAttribute('type') || 'text').toLowerCase()
      if (['button', 'submit', 'reset', 'image'].includes(type)) return 'button'
      if (type === 'checkbox') return 'checkbox'
      if (type === 'radio') return 'radio'
      if (type === 'range') return 'slider'
      if (type === 'number') return 'spinbutton'
      if (type === 'search') return 'searchbox'
      if (type === 'hidden') return null
      return 'textbox'
    },
  }
  const NAME_FROM_CONTENT = new Set(['button', 'link', 'heading', 'option', 'cell', 'columnheader', 'menuitem', 'tab',
    'checkbox', 'radio', 'listitem', 'switch', 'menuitemradio', 'menuitemcheckbox', 'treeitem', 'row'])
  const FIELDS = new Set(['input', 'textarea', 'select'])
  const clean = (text) => String(text ?? '').replace(/\s+/g, ' ').trim()
  const short = (text) => {
    const value = clean(text)
    return value ? value.slice(0, 500) : null
  }
  const contentOf = (el) => clean(el.innerText !== undefined && el.innerText !== null ? el.innerText : el.textContent)
  const byId = (id) => (doc.getElementById ? doc.getElementById(id) : null)
  const labelledBy = (el) => {
    const ids = clean(el.getAttribute('aria-labelledby'))
    if (!ids) return ''
    return clean(ids.split(' ').map((id) => {
      const found = byId(id)
      return found ? contentOf(found) : ''
    }).join(' '))
  }
  const labelOf = (el, tag) => {
    const aria = clean(el.getAttribute('aria-label'))
    if (aria) return aria
    const by = labelledBy(el)
    if (by) return by
    if (!FIELDS.has(tag)) return ''
    const labels = el.labels ? Array.from(el.labels) : []
    const text = clean(labels.map((label) => contentOf(label)).join(' '))
    if (text) return text
    const id = el.getAttribute('id')
    if (id && doc.querySelector) {
      const found = doc.querySelector(`label[for="${String(id).replace(/"/g, '\\"')}"]`)
      if (found) return contentOf(found)
    }
    return ''
  }
  const roleOf = (el, tag) => {
    const explicit = clean(el.getAttribute('role')).split(' ')[0]
    if (explicit) return explicit
    const implicit = IMPLICIT[tag]
    return implicit ? implicit(el) : null
  }
  const nameOf = (el, tag, role, label) => {
    if (label) return label
    if (tag === 'img') return clean(el.getAttribute('alt'))
    if (tag === 'input') {
      const type = String(el.getAttribute('type') || 'text').toLowerCase()
      if (['button', 'submit', 'reset'].includes(type)) return clean(el.getAttribute('value') || el.value || '')
    }
    const title = clean(el.getAttribute('title'))
    if (role && NAME_FROM_CONTENT.has(role)) return contentOf(el) || title
    return title
  }
  const ownText = (el) => {
    let found = false
    for (const child of Array.from(el.childNodes || [])) {
      if (child.nodeType === 3 && clean(child.nodeValue)) {
        found = true
        break
      }
    }
    return found ? contentOf(el) : ''
  }
  const skipped = (el) => {
    const id = el.getAttribute('id') || ''
    return el.getAttribute('data-ehx-overlay') !== null || id.startsWith('ehx-') || ['script', 'style', 'noscript', 'template', 'head'].includes(el.tagName.toLowerCase())
  }
  const nodes = []
  const visit = (el, parent, hidden) => {
    if (nodes.length >= max || skipped(el)) return
    const style = win.getComputedStyle(el)
    if (style.display === 'none') return
    const faded = hidden || Number(style.opacity) === 0
    const invisible = faded || style.visibility === 'hidden' || style.visibility === 'collapse'
    const tag = el.tagName.toLowerCase()
    let index = parent
    if (!invisible) {
      const rect = el.getBoundingClientRect()
      const x = rect.left + offsetX
      const y = rect.top + offsetY
      const onScreen = rect.width > 0 && rect.height > 0 && x < regionW && y < regionH && x + rect.width > 0 && y + rect.height > 0
      if (onScreen) {
        const role = roleOf(el, tag)
        const label = labelOf(el, tag)
        const name = role ? nameOf(el, tag, role, label) : ''
        const testId = el.getAttribute('data-testid')
        const placeholder = el.getAttribute('placeholder')
        const text = ownText(el)
        const named = role && name
        const structural = role && el.getAttribute('role') !== null
        if (testId || named || structural || label || placeholder || text) {
          index = nodes.length
          const node = { kind: tag, box: [x, y, rect.width, rect.height], parent }
          if (testId) node.testId = testId
          if (role) node.role = role
          if (name) node.name = short(name)
          if (label) node.label = short(label)
          if (placeholder) node.placeholder = short(placeholder)
          if (text) node.text = short(text)
          nodes.push(node)
        }
      }
    }
    for (const child of Array.from(el.children || [])) visit(child, index, faded)
  }
  if (doc.body) visit(doc.body, null, false)
  return { width: regionW, height: regionH, nodes }
}

export function domBoxMapSource(opts) {
  return `(${domBoxMap.toString()})(document, window, ${JSON.stringify(opts ?? {})})`
}

export function pngSize(buffer) {
  const magic = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]
  if (!buffer || buffer.length < 24 || magic.some((byte, index) => buffer[index] !== byte)) throw new Error('not a PNG image')
  return { width: buffer.readUInt32BE(16), height: buffer.readUInt32BE(20) }
}

function rounded(value) {
  const number = Math.round(Number(value) * 1000) / 1000
  return Object.is(number, -0) ? 0 : number
}

export function scaledBoxMap(walk, size, { source = 'web' } = {}) {
  const factor = walk.width > 0 ? size.width / walk.width : 1
  return {
    schema: BOXES_SCHEMA,
    size: [size.width, size.height],
    scale: rounded(factor),
    source,
    nodes: walk.nodes.slice(0, MAX_NODES).map((node) => ({ ...node, box: node.box.map((value) => rounded(value * factor)) })),
  }
}

export function cssStep(walk, t, action = null) {
  return {
    t: rounded(t),
    action,
    boxes: { schema: BOXES_SCHEMA, size: [walk.width, walk.height], scale: 1, source: 'web', nodes: walk.nodes.slice(0, MAX_NODES) },
  }
}

export function recordingDocument({ video, units, steps, events = [], crop = null, trimmed = false, duration = null }) {
  const doc = { schema: RECORDING_SCHEMA, units, video, trimmed: Boolean(trimmed), crop, events: events.map(rounded), steps }
  if (Number.isFinite(duration) && duration > 0) doc.duration = rounded(duration)
  return doc
}
