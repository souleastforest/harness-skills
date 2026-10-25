/**
 * Activation-level tests: config resolution/validation and the `apply()` wiring.
 * `index.js` imports nothing from `@deepseek-ai/*`, so it runs under plain
 * `node --test` exactly as it does inside the Harness.
 */
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { DEFAULT_CONFIG, apply, inject, name, resolveConfig } from '../index.js'

/** A minimal ctx whose `web` records provider registrations. */
function fakeContext(overrides = {}) {
  const registered = { search: [], fetch: [] }
  const web = {
    searchProviders: new Map([['deepseek-official', { id: 'deepseek-official', available: () => true }]]),
    fetchProviders: new Map([['http', { id: 'http', available: () => true }]]),
    registerSearchProvider(provider) { registered.search.push(provider); return () => {} },
    registerFetchProvider(provider) { registered.fetch.push(provider); return () => {} },
    ...overrides.web,
  }
  const ctx = {
    web,
    get(service) {
      if (overrides.get !== undefined) return overrides.get(service)
      if (service === 'credentials') return { resolve: async (ref) => (ref === 'DEEPSEEK_API_KEY' ? { value: 'sk-test', source: 'file' } : undefined) }
      return undefined
    },
  }
  return { ctx, registered }
}

test('resolveConfig applies every default to an absent row config', () => {
  const resolved = resolveConfig(undefined)
  assert.deepEqual(resolved, DEFAULT_CONFIG)
  assert.equal(resolved.apiKeyEnv, 'DEEPSEEK_API_KEY')
  assert.equal(resolved.timeoutMs, 15_000)
  assert.equal(resolved.maxRetries, 1)
  assert.equal(resolved.searchFallbackProvider, 'deepseek-official')
  assert.equal(resolved.fetchFallbackProvider, 'http')
  assert.equal(resolveConfig({}).searchCount, 10)
})

test('resolveConfig keeps supplied values and ignores explicit undefined', () => {
  const resolved = resolveConfig({ timeoutMs: 2_000, searchFallback: false, baseURL: 'https://gw.internal' })
  assert.equal(resolved.timeoutMs, 2_000)
  assert.equal(resolved.searchFallback, false)
  assert.equal(resolved.baseURL, 'https://gw.internal')
  const withUndefined = resolveConfig({ timeoutMs: undefined })
  assert.equal(withUndefined.timeoutMs, 15_000)
})

test('resolveConfig rejects unknown keys so a typo fails activation loudly', () => {
  assert.throws(() => resolveConfig({ timeOutMs: 5_000 }), /unknown config key.*timeOutMs/u)
  assert.throws(() => resolveConfig({ searchFallBack: true }), /unknown config key/u)
})

test('resolveConfig rejects out-of-range and mistyped values', () => {
  assert.throws(() => resolveConfig({ timeoutMs: 0 }), /timeoutMs must be a positive integer/u)
  assert.throws(() => resolveConfig({ timeoutMs: 1.5 }), /timeoutMs must be a positive integer/u)
  assert.throws(() => resolveConfig({ maxRetries: -1 }), /maxRetries must be an integer between 0 and 10/u)
  assert.throws(() => resolveConfig({ maxRetries: 11 }), /maxRetries must be an integer/u)
  assert.throws(() => resolveConfig({ searchCount: 51 }), /searchCount must be an integer between 1 and 50/u)
  assert.throws(() => resolveConfig({ searchNeedContent: 'true' }), /searchNeedContent must be true or false/u)
  assert.throws(() => resolveConfig({ searchFallbackProvider: '' }), /searchFallbackProvider must be a non-empty string/u)
  assert.throws(() => resolveConfig({ apiKeyEnv: '' }), /apiKeyEnv must be a non-empty string/u)
  assert.throws(() => resolveConfig([]), /config must be a mapping/u)
})

test('a disabled fallback does not require a provider id', () => {
  const resolved = resolveConfig({ searchFallback: false, searchFallbackProvider: '' })
  assert.equal(resolved.searchFallback, false)
})

test('apply registers both providers under the deepseek-tools id', () => {
  const { ctx, registered } = fakeContext()
  apply(ctx, {})
  assert.equal(registered.search.length, 1)
  assert.equal(registered.fetch.length, 1)
  assert.equal(registered.search[0].id, 'deepseek-tools')
  assert.equal(registered.fetch[0].id, 'deepseek-tools')
  assert.equal(registered.search[0].available(), true)
  assert.equal(registered.fetch[0].available(), true)
})

test('apply honours $DEEPSEEK_TOOLS_BASE_URL and rejects an invalid base URL', () => {
  const { ctx, registered } = fakeContext()
  const previous = process.env.DEEPSEEK_TOOLS_BASE_URL
  process.env.DEEPSEEK_TOOLS_BASE_URL = 'https://gateway.internal/deepseek'
  try {
    apply(ctx, {})
    assert.equal(registered.search.length, 1)
  } finally {
    if (previous === undefined) delete process.env.DEEPSEEK_TOOLS_BASE_URL
    else process.env.DEEPSEEK_TOOLS_BASE_URL = previous
  }
  assert.throws(() => apply(fakeContext().ctx, { baseURL: 'not a url' }), /not an absolute http\(s\) URL/u)
})

test('name and inject are the contract the Loader reads', () => {
  assert.equal(name, 'web-deepseek-tools')
  assert.deepEqual(inject, ['web'])
})
