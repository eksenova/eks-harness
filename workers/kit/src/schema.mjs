const TYPE_CHECKS = {
  string: (value) => typeof value === 'string',
  integer: (value) => Number.isInteger(value),
  number: (value) => typeof value === 'number' && Number.isFinite(value),
  boolean: (value) => typeof value === 'boolean',
  null: (value) => value === null,
  array: (value) => Array.isArray(value),
  object: (value) => value !== null && typeof value === 'object' && !Array.isArray(value),
}

function resolveRef(schema, defs) {
  if (!schema?.$ref) return schema
  const name = schema.$ref.split('/').pop()
  const found = defs?.[name]
  if (!found) throw new Error(`schema reference ${schema.$ref} is not defined`)
  return found
}

function typeOf(value) {
  if (value === null) return 'null'
  if (Array.isArray(value)) return 'array'
  return typeof value
}

function show(path) {
  return path || 'args'
}

export function validate(schema, value, defs = {}, path = '') {
  const errors = []
  check(schema, value, defs, path, errors)
  return errors
}

function check(schema, value, defs, path, errors) {
  if (!schema || typeof schema !== 'object' || Object.keys(schema).length === 0) return
  schema = resolveRef(schema, defs)
  if (schema.anyOf) {
    const matched = schema.anyOf.some((option) => validate(option, value, defs, path).length === 0)
    if (!matched) errors.push(`${show(path)}: ${JSON.stringify(value)} matches none of the allowed shapes`)
    return
  }
  if (schema.enum && !schema.enum.some((option) => option === value)) {
    errors.push(`${show(path)}: ${JSON.stringify(value)} is not one of ${schema.enum.map((v) => JSON.stringify(v)).join(', ')}`)
    return
  }
  if (schema.type) {
    const types = Array.isArray(schema.type) ? schema.type : [schema.type]
    if (!types.some((type) => TYPE_CHECKS[type]?.(value))) {
      errors.push(`${show(path)}: expected ${types.join(' or ')}, got ${typeOf(value)}`)
      return
    }
  }
  if (TYPE_CHECKS.object(value) && (schema.properties || schema.additionalProperties !== undefined)) {
    const properties = schema.properties ?? {}
    for (const key of schema.required ?? []) {
      if (!(key in value)) errors.push(`${show(path)}: missing ${key}`)
    }
    for (const [key, item] of Object.entries(value)) {
      const child = path ? `${path}.${key}` : key
      if (key in properties) check(properties[key], item, defs, child, errors)
      else if (schema.additionalProperties === false) errors.push(`${show(path)}: unknown field ${key}`)
      else if (schema.additionalProperties && typeof schema.additionalProperties === 'object') {
        check(schema.additionalProperties, item, defs, child, errors)
      }
    }
  }
  if (Array.isArray(value)) {
    if (schema.minItems !== undefined && value.length < schema.minItems) {
      errors.push(`${show(path)}: needs at least ${schema.minItems} items`)
    }
    if (schema.maxItems !== undefined && value.length > schema.maxItems) {
      errors.push(`${show(path)}: takes at most ${schema.maxItems} items`)
    }
    value.forEach((item, index) => {
      const itemSchema = schema.prefixItems?.[index] ?? schema.items
      if (itemSchema) check(itemSchema, item, defs, `${path}[${index}]`, errors)
    })
  }
}
