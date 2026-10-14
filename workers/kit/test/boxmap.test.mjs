import { describe, expect, it } from 'vitest'

import { BOXES_SCHEMA, cssStep, domBoxMap, domBoxMapSource, pngSize, recordingDocument, scaledBoxMap } from '../src/boxmap.mjs'

function text(value) {
  return { nodeType: 3, nodeValue: value }
}

function el(tag, attrs = {}, kids = [], { rect = [0, 0, 0, 0], style = {} } = {}) {
  const children = kids.filter((kid) => typeof kid !== 'string')
  const childNodes = kids.map((kid) => (typeof kid === 'string' ? text(kid) : kid))
  const node = {
    nodeType: 1,
    tagName: tag.toUpperCase(),
    children,
    childNodes,
    style: { display: 'block', visibility: 'visible', opacity: '1', ...style },
    getAttribute: (name) => (name in attrs ? String(attrs[name]) : null),
    getBoundingClientRect: () => ({ left: rect[0], top: rect[1], width: rect[2], height: rect[3] }),
  }
  Object.defineProperty(node, 'textContent', { get: () => childNodes.map((kid) => (kid.nodeType === 3 ? kid.nodeValue : kid.textContent)).join('') })
  return node
}

function page(body, extra = {}) {
  const ids = new Map()
  const index = (node) => {
    const id = node.getAttribute?.('id')
    if (id) ids.set(id, node)
    for (const child of node.children ?? []) index(child)
  }
  index(body)
  const doc = { body, documentElement: { scrollWidth: 800, scrollHeight: 1200 }, getElementById: (id) => ids.get(id) ?? null, querySelector: () => null }
  const win = { innerWidth: 400, innerHeight: 300, scrollX: 0, scrollY: 100, getComputedStyle: (node) => node.style, ...extra }
  return { doc, win }
}

function fixture() {
  const saveLabel = el('span', {}, ['Kaydet'], { rect: [302, 262, 40, 20] })
  return el('body', {}, [
    el('header', { 'data-testid': 'header' }, [el('h1', {}, ['Faturalar'], { rect: [10, 10, 80, 20] })], { rect: [0, 0, 400, 40] }),
    el('ul', { role: 'list' }, [
      el('li', {}, [el('button', {}, ['Sil'], { rect: [10, 60, 60, 20] })], { rect: [0, 55, 400, 30] }),
      el('li', {}, [el('button', {}, ['Sil'], { rect: [10, 200, 60, 20] })], { rect: [0, 195, 400, 30] }),
    ], { rect: [0, 50, 400, 200] }),
    el('button', { 'data-testid': 'save' }, [saveLabel], { rect: [300, 260, 80, 30] }),
    el('input', { placeholder: 'Ad Soyad', 'aria-label': 'Adı' }, [], { rect: [100, 260, 150, 30] }),
    el('div', { 'data-testid': 'gone' }, ['Gizli'], { rect: [0, 0, 10, 10], style: { display: 'none' } }),
    el('div', {}, [el('p', {}, ['Saydam'], { rect: [0, 0, 10, 10] })], { rect: [0, 0, 10, 10], style: { opacity: '0' } }),
    el('p', {}, ['Aşağıda'], { rect: [0, 900, 100, 20] }),
    el('div', { id: 'ehx-pointer-dot', 'data-testid': 'dot' }, [], { rect: [5, 5, 5, 5] }),
  ], { rect: [0, 0, 400, 300] })
}

describe('box maps', () => {
  it('walks the visible DOM with test ids, roles, names, labels, placeholders and own text', () => {
    const { doc, win } = page(fixture())
    const walk = domBoxMap(doc, win, {})
    expect(walk.width).toBe(400)
    const byTest = Object.fromEntries(walk.nodes.filter((node) => node.testId).map((node) => [node.testId, node]))
    expect(Object.keys(byTest).sort()).toEqual(['header', 'save'])
    expect(byTest.save).toMatchObject({ kind: 'button', role: 'button', name: 'Kaydet', box: [300, 260, 80, 30] })
    expect(byTest.save.text).toBeUndefined()
    const span = walk.nodes.find((node) => node.kind === 'span')
    expect(span.text).toBe('Kaydet')
    expect(walk.nodes[span.parent]).toBe(byTest.save)
    const heading = walk.nodes.find((node) => node.role === 'heading')
    expect(heading).toMatchObject({ name: 'Faturalar', text: 'Faturalar' })
    expect(walk.nodes[heading.parent].testId).toBe('header')
    const input = walk.nodes.find((node) => node.kind === 'input')
    expect(input).toMatchObject({ role: 'textbox', label: 'Adı', name: 'Adı', placeholder: 'Ad Soyad' })
    const sil = walk.nodes.filter((node) => node.text === 'Sil')
    expect(sil).toHaveLength(2)
    expect(walk.nodes[walk.nodes[sil[0].parent].parent].role).toBe('list')
    expect(walk.nodes.some((node) => ['Gizli', 'Saydam', 'Aşağıda'].includes(node.text))).toBe(false)
  })

  it('offsets full page and element captures and honours the node cap', () => {
    const { doc, win } = page(fixture())
    const full = domBoxMap(doc, win, { full: true })
    expect(full.width).toBe(800)
    expect(full.nodes.find((node) => node.testId === 'save').box).toEqual([300, 360, 80, 30])
    expect(full.nodes.some((node) => node.text === 'Aşağıda')).toBe(true)
    const element = domBoxMap(doc, win, { offsetX: -300, offsetY: -260, width: 80, height: 30 })
    expect(element.nodes.map((node) => node.box)).toEqual([[0, 0, 80, 30], [2, 2, 40, 20]])
    expect(domBoxMap(doc, win, { max: 2 }).nodes).toHaveLength(2)
  })

  it('scales walks to image pixels and builds recording documents', () => {
    const { doc, win } = page(fixture())
    const walk = domBoxMap(doc, win, {})
    const map = scaledBoxMap(walk, { width: 800, height: 600 })
    expect(map).toMatchObject({ schema: BOXES_SCHEMA, size: [800, 600], scale: 2, source: 'web' })
    expect(map.nodes.find((node) => node.testId === 'save').box).toEqual([600, 520, 160, 60])
    const doc2 = recordingDocument({ video: 'tour.mp4', units: 'css', steps: [cssStep(walk, 1.23456, 'ui.tap')], events: [1.2], trimmed: true })
    expect(doc2).toMatchObject({ schema: 'eks/recording-boxes@1', units: 'css', trimmed: true, events: [1.2] })
    expect(doc2.steps[0]).toMatchObject({ t: 1.235, action: 'ui.tap', boxes: { size: [400, 300], scale: 1 } })
  })

  it('reads PNG sizes and evaluates as a self-contained page script', () => {
    const header = Buffer.alloc(24)
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]).copy(header)
    header.write('IHDR', 12)
    header.writeUInt32BE(1170, 16)
    header.writeUInt32BE(2532, 20)
    expect(pngSize(header)).toEqual({ width: 1170, height: 2532 })
    expect(() => pngSize(Buffer.from('nope'))).toThrow('not a PNG')
    const { doc, win } = page(fixture())
    const run = new Function('document', 'window', `return ${domBoxMapSource({ max: 5 })}`)
    expect(run(doc, win).nodes).toHaveLength(5)
  })
})
