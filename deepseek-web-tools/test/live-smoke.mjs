/**
 * Live smoke test for the DeepSeek Search / Fetch tools API.
 *
 * Run manually with a real key in the environment (never hardcode it):
 *   $env:DEEPSEEK_API_KEY = '<key>'; node test/live-smoke.mjs
 *
 * It exercises the provider code paths — request shape, result mapping, and the
 * fallback chain with an unreachable endpoint — against the real service. It is
 * deliberately NOT part of `node --test`, because it spends the account's
 * Search/Fetch quota and needs the network.
 */
import assert from 'node:assert/strict'
import {
  createDeepSeekFetchProvider,
  createDeepSeekSearchProvider,
} from '../lib/providers.js'

const apiKey = process.env.DEEPSEEK_API_KEY
if (typeof apiKey !== 'string' || apiKey.length === 0) {
  console.log('SKIP: DEEPSEEK_API_KEY is not set; live smoke test needs a real key.')
  process.exit(0)
}

function makeError(message, code = 'WEB_PROVIDER_ERROR', options = {}) {
  const error = options.cause === undefined ? new Error(message) : new Error(message, { cause: options.cause })
  error.code = code
  return error
}

function makeOptions(overrides = {}) {
  return {
    baseURL: 'https://api.deepseek.com',
    timeoutMs: 20_000,
    maxRetries: 1,
    searchCount: 5,
    searchNeedContent: false,
    searchLocalization: undefined,
    searchFallback: true,
    searchFallbackProvider: 'deepseek-official',
    fetchFallback: true,
    fetchFallbackProvider: 'http',
    fetchMaxContentChars: 100_000,
    snippetChars: 600,
    apiKey,
    apiKeyEnv: 'DEEPSEEK_API_KEY',
    resolveApiKey: async () => apiKey,
    resolveAccountToken: async () => undefined,
    ...overrides,
  }
}

function deps(options, lookupFallback, record = () => {}) {
  return {
    id: 'deepseek-tools',
    resolveOptions: () => options,
    createError: makeError,
    fetchImpl: (...args) => globalThis.fetch(...args),
    lookupFallback,
    record,
  }
}

async function main() {
  const events = []

  // 1. Real search.
  const search = createDeepSeekSearchProvider(deps(makeOptions(), () => undefined, (type, payload) => events.push({ type, payload })))
  const started = Date.now()
  const searchResult = await search.search({ query: 'DeepSeek V4 发布 2026', maxResults: 5 })
  const elapsed = Date.now() - started
  assert.ok(searchResult.sources.length > 0, 'live search returned no sources')
  assert.ok(searchResult.sources.every((source) => typeof source.url === 'string' && /^https?:\/\//u.test(source.url)))
  assert.ok(searchResult.sources.every((source) => source.snippet === undefined || source.snippet.length > 0))
  assert.ok(events.some((event) => event.type === 'web/deepseek-tools-request'))
  console.log(`OK search: ${searchResult.sources.length} sources in ${elapsed} ms`)
  for (const source of searchResult.sources.slice(0, 3)) console.log(`   - ${source.title ?? '(no title)'} ${source.url}`)

  // 2. Real fetch.
  const fetchProvider = createDeepSeekFetchProvider(deps(makeOptions(), () => undefined))
  const fetchResult = await fetchProvider.fetch({ url: 'https://example.com' })
  assert.equal(fetchResult.statusCode, 200)
  assert.equal(fetchResult.body.kind, 'text')
  assert.ok(fetchResult.body.content.length > 0, 'live fetch returned an empty body')
  console.log(`OK fetch: ${fetchResult.body.content.length} markdown chars from example.com`)

  // 3. Fallback: unreachable primary endpoint must hand over to the fallback provider.
  let fallbackCalls = 0
  const fallbackResult = { sources: [{ url: 'https://fallback.example/result', title: 'fallback' }], truncated: false }
  const fallbackEvents = []
  const fallbackSearch = createDeepSeekSearchProvider(deps(
    { ...makeOptions(), baseURL: 'http://127.0.0.1:9', timeoutMs: 2_000, maxRetries: 0 },
    (id) => (id === 'deepseek-official'
      ? { id, available: () => true, search: async () => { fallbackCalls += 1; return fallbackResult } }
      : undefined),
    (type, payload) => fallbackEvents.push({ type, payload }),
  ))
  const fallbackOutcome = await fallbackSearch.search({ query: 'fallback probe' })
  assert.equal(fallbackCalls, 1, 'the fallback provider was not used')
  assert.deepEqual(fallbackOutcome, fallbackResult)
  assert.ok(fallbackEvents.some((event) => event.type === 'web/deepseek-tools-fallback'), 'no fallback event recorded')
  console.log('OK fallback: unreachable endpoint handed over to the fallback provider')

  // 4. An auth rejection at the primary must also fall back: an invalid key
  //    makes the real endpoint answer 401, which is not retried but is handed
  //    to the fallback provider.
  let authFallbackCalls = 0
  const invalidKeyProvider = createDeepSeekSearchProvider(deps(
    { ...makeOptions(), apiKey: 'sk-definitely-invalid' },
    (id) => (id === 'deepseek-official'
      ? { id, available: () => true, search: async () => { authFallbackCalls += 1; return fallbackResult } }
      : undefined),
  ))
  const invalidKeyOutcome = await invalidKeyProvider.search({ query: 'auth probe' })
  assert.equal(authFallbackCalls, 1, 'a 401 did not fall back')
  assert.deepEqual(invalidKeyOutcome, fallbackResult)
  console.log('OK fallback: an endpoint rejection (401) handed over to the fallback provider')

  console.log('\nLIVE SMOKE PASSED')
}

main().catch((error) => {
  console.error('LIVE SMOKE FAILED:', error)
  process.exit(1)
})
