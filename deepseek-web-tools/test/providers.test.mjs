import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createDeepSeekFetchProvider, createDeepSeekSearchProvider } from '../lib/providers.js'

/* ───────────────────────────── harness ───────────────────────────── */

/** Production-shaped provider error for the injected `createError`. */
function makeError(message, code = 'WEB_PROVIDER_ERROR', options = {}) {
  const error = options.cause === undefined ? new Error(message) : new Error(message, { cause: options.cause })
  error.code = code
  return error
}

/** One resolved-options snapshot with every field the providers read. */
function makeOptions(overrides = {}) {
  return {
    baseURL: 'https://api.deepseek.com',
    timeoutMs: 1_000,
    maxRetries: 1,
    searchCount: 10,
    searchNeedContent: false,
    searchLocalization: undefined,
    searchFallback: true,
    searchFallbackProvider: 'deepseek-official',
    fetchFallback: true,
    fetchFallbackProvider: 'http',
    fetchMaxContentChars: 1_000,
    snippetChars: 600,
    apiKey: 'sk-test',
    apiKeyEnv: 'DEEPSEEK_API_KEY',
    resolveApiKey: async () => 'sk-test',
    resolveAccountToken: async () => undefined,
    ...overrides,
  }
}

/** A Response stand-in for the injected fetch. */
function jsonResponse(status, body) {
  const text = typeof body === 'string' ? body : JSON.stringify(body)
  return {
    status,
    ok: status >= 200 && status < 300,
    json: async () => (typeof body === 'string' ? JSON.parse(body) : body),
    text: async () => text,
  }
}

/** Recording fetch stub that answers from a queue of factories. */
function recordingFetch(handlers) {
  const calls = []
  const fetchImpl = async (input, init) => {
    calls.push({ url: input, init })
    const handler = handlers[Math.min(calls.length - 1, handlers.length - 1)]
    return handler(input, init)
  }
  return { fetchImpl, calls }
}

/** Build search-provider dependencies. */
function searchDeps({ options, fetchImpl, lookupFallback, record = () => {} }) {
  return {
    id: 'deepseek-tools',
    resolveOptions: () => options,
    createError: makeError,
    fetchImpl,
    lookupFallback,
    record,
  }
}

/** Build fetch-provider dependencies. */
function fetchDeps({ options, fetchImpl, lookupFallback, record = () => {} }) {
  return {
    id: 'deepseek-tools',
    resolveOptions: () => options,
    createError: makeError,
    fetchImpl,
    lookupFallback,
    record,
  }
}

/* ───────────────────────────── search ───────────────────────────── */

test('search dispatches the documented request and maps sources', async () => {
  const { fetchImpl, calls } = recordingFetch([
    () => jsonResponse(200, {
      results: [
        { url: 'https://example.com/a', title: 'A', snippet: 'sa', summary: 'long', date: '2026-06-01T00:00:00+00:00', rerank_score: 2.4, authority: 3 },
        { url: 'https://example.com/b', title: 'B', summary: 'sb' },
      ],
    }),
  ])
  const provider = createDeepSeekSearchProvider(searchDeps({ options: makeOptions(), fetchImpl }))
  const result = await provider.search({ query: '  deepseek  ', maxResults: 3 })

  assert.equal(calls.length, 1)
  assert.equal(calls[0].url, 'https://api.deepseek.com/v1/tools/search')
  assert.equal(calls[0].init.method, 'POST')
  assert.equal(calls[0].init.headers.authorization, 'Bearer sk-test')
  assert.equal(calls[0].init.headers['x-api-key'], 'sk-test')
  assert.equal(calls[0].init.redirect, 'error')
  assert.deepEqual(JSON.parse(calls[0].init.body), { query: 'deepseek', count: 3 })
  assert.deepEqual(result.sources.map((source) => source.url), ['https://example.com/a', 'https://example.com/b'])
  assert.equal(result.sources[0].snippet, 'sa')
  assert.equal(result.sources[1].snippet, 'sb')
  assert.equal(result.truncated, false)
})

test('search retries a retryable status and succeeds on the second attempt', async () => {
  const { fetchImpl, calls } = recordingFetch([
    () => jsonResponse(503, { error: { message: 'busy' } }),
    () => jsonResponse(200, { results: [{ url: 'https://example.com/ok' }] }),
  ])
  const provider = createDeepSeekSearchProvider(searchDeps({ options: makeOptions({ maxRetries: 1 }), fetchImpl }))
  const result = await provider.search({ query: 'q' })
  assert.equal(calls.length, 2)
  assert.equal(result.sources[0].url, 'https://example.com/ok')
})

test('search falls back to the registered fallback provider when retries are exhausted', async () => {
  const { fetchImpl, calls } = recordingFetch([() => jsonResponse(503, { error: { message: 'busy' } })])
  const events = []
  const fallbackResult = { sources: [{ url: 'https://legacy.example/result' }], truncated: false }
  let fallbackRequest
  const provider = createDeepSeekSearchProvider(searchDeps({
    options: makeOptions({ maxRetries: 0 }),
    fetchImpl,
    lookupFallback: (id) => (id === 'deepseek-official'
      ? { id, available: () => true, search: async (request) => { fallbackRequest = request; return fallbackResult } }
      : undefined),
    record: (type, payload) => events.push({ type, payload }),
  }))
  const result = await provider.search({ query: 'q' })
  assert.equal(calls.length, 1)
  assert.equal(result, fallbackResult)
  assert.equal(fallbackRequest.query, 'q')
  assert.ok(events.some((event) => event.type === 'web/deepseek-tools-fallback'))
  assert.ok(events.some((event) => event.type === 'web/deepseek-tools-request'))
})

test('search throws the primary failure when fallback is disabled', async () => {
  const { fetchImpl } = recordingFetch([() => jsonResponse(500, { error: { message: 'boom' } })])
  const provider = createDeepSeekSearchProvider(searchDeps({
    options: makeOptions({ maxRetries: 0, searchFallback: false }),
    fetchImpl,
  }))
  await assert.rejects(provider.search({ query: 'q' }), (error) => {
    assert.equal(error.code, 'WEB_PROVIDER_ERROR')
    assert.match(error.message, /HTTP 500/u)
    assert.match(error.message, /boom/u)
    assert.match(error.message, /\/v1\/tools\/search/u)
    return true
  })
})

test('search explains a missing fallback provider instead of hiding the note', async () => {
  const { fetchImpl } = recordingFetch([() => jsonResponse(500, {})])
  const provider = createDeepSeekSearchProvider(searchDeps({
    options: makeOptions({ maxRetries: 0 }),
    fetchImpl,
    lookupFallback: () => undefined,
  }))
  await assert.rejects(provider.search({ query: 'q' }), /fallback search provider "deepseek-official" is not registered/u)
})

test('search does not fall back on caller cancellation', async () => {
  const { fetchImpl, calls } = recordingFetch([() => jsonResponse(200, { results: [] })])
  const controller = new AbortController()
  controller.abort(new Error('user stopped'))
  let fallbackCalls = 0
  const provider = createDeepSeekSearchProvider(searchDeps({
    options: makeOptions(),
    fetchImpl,
    lookupFallback: () => ({ id: 'deepseek-official', available: () => true, search: async () => { fallbackCalls += 1 } }),
  }))
  await assert.rejects(provider.search({ query: 'q' }, controller.signal), (error) => {
    assert.equal(error.code, 'WEB_ABORTED')
    return true
  })
  assert.equal(calls.length, 0)
  assert.equal(fallbackCalls, 0)
})

test('search upgrades an account-token 401 to the API key once', async () => {
  const { fetchImpl, calls } = recordingFetch([
    () => jsonResponse(401, { error: { message: 'expired ticket' } }),
    () => jsonResponse(200, { results: [{ url: 'https://example.com/a' }] }),
  ])
  const provider = createDeepSeekSearchProvider(searchDeps({
    options: makeOptions({ resolveAccountToken: async () => 'acct-ticket' }),
    fetchImpl,
  }))
  const result = await provider.search({ query: 'q' })
  assert.equal(calls.length, 2)
  assert.equal(calls[0].init.headers['x-dsh-auth-token'], 'acct-ticket')
  assert.equal(calls[0].init.headers.authorization, undefined)
  assert.equal(calls[1].init.headers.authorization, 'Bearer sk-test')
  assert.equal(result.sources[0].url, 'https://example.com/a')
})

test('search maps a client timeout to WEB_PROVIDER_TIMEOUT, skips retry, and falls back', async () => {
  let calls = 0
  // AbortSignal.timeout() is unref'd, so an unconditional "wait for abort"
  // promise lets node:test decide the event loop is empty. A referenced
  // guard timer keeps the loop alive and fails the test loudly if our own
  // deadline ever stops firing.
  const fetchImpl = (_input, init) => new Promise((_resolve, reject) => {
    calls += 1
    const guard = setTimeout(() => reject(new Error('provider did not enforce its timeout')), 2_000)
    init.signal.addEventListener('abort', () => {
      clearTimeout(guard)
      reject(init.signal.reason)
    }, { once: true })
  })
  const fallbackResult = { sources: [{ url: 'https://legacy.example/result' }], truncated: false }
  const provider = createDeepSeekSearchProvider(searchDeps({
    options: makeOptions({ timeoutMs: 20, maxRetries: 3 }),
    fetchImpl,
    lookupFallback: () => ({ id: 'deepseek-official', available: () => true, search: async () => fallbackResult }),
  }))
  const result = await provider.search({ query: 'q' })
  assert.equal(calls, 1, 'a deadline is not retried')
  assert.equal(result, fallbackResult)
})

test('search reports every failure when the fallback fails too', async () => {
  const { fetchImpl } = recordingFetch([() => jsonResponse(503, {})])
  const provider = createDeepSeekSearchProvider(searchDeps({
    options: makeOptions({ maxRetries: 0 }),
    fetchImpl,
    lookupFallback: () => ({ id: 'deepseek-official', available: () => true, search: async () => { throw makeError('legacy down', 'WEB_PROVIDER_ERROR') } }),
  }))
  await assert.rejects(provider.search({ query: 'q' }), (error) => {
    assert.match(error.message, /HTTP 503/u)
    assert.match(error.message, /legacy down/u)
    return true
  })
})

test('search surfaces credential-missing without a fallback when fallback is disabled', async () => {
  const { fetchImpl, calls } = recordingFetch([() => jsonResponse(200, { results: [] })])
  const provider = createDeepSeekSearchProvider(searchDeps({
    options: makeOptions({ apiKey: undefined, resolveApiKey: async () => undefined, searchFallback: false }),
    fetchImpl,
  }))
  await assert.rejects(provider.search({ query: 'q' }), (error) => {
    assert.equal(error.code, 'WEB_PROVIDER_CREDENTIAL_MISSING')
    return true
  })
  assert.equal(calls.length, 0)
})

test('search exposes availability from the resolved base URL', () => {
  const provider = createDeepSeekSearchProvider(searchDeps({ options: makeOptions(), fetchImpl: async () => jsonResponse(200, {}) }))
  assert.equal(provider.available(), true)
  const broken = createDeepSeekSearchProvider(searchDeps({ options: makeOptions({ baseURL: '' }), fetchImpl: async () => jsonResponse(200, {}) }))
  assert.equal(broken.available(), false)
})

/* ───────────────────────────── fetch ───────────────────────────── */

test('fetch dispatches the URL and maps Markdown content', async () => {
  const { fetchImpl, calls } = recordingFetch([
    () => jsonResponse(200, { title: 'Example', content: 'Body **text**' }),
  ])
  const provider = createDeepSeekFetchProvider(fetchDeps({ options: makeOptions(), fetchImpl }))
  const result = await provider.fetch({ url: 'https://example.com/article' })
  assert.equal(calls[0].url, 'https://api.deepseek.com/v1/tools/fetch')
  assert.deepEqual(JSON.parse(calls[0].init.body), { url: 'https://example.com/article' })
  assert.deepEqual(result, {
    url: 'https://example.com/article',
    statusCode: 200,
    body: { kind: 'text', content: '# Example\n\nBody **text**' },
    truncated: false,
  })
})

test('fetch falls back to the local HTTP provider on a documented 404', async () => {
  const { fetchImpl } = recordingFetch([() => jsonResponse(404, { error: { message: 'no usable content' } })])
  const localResult = { url: 'https://example.com/article', statusCode: 200, body: { kind: 'html', content: '<h1>x</h1>' }, truncated: false }
  let usedProvider
  const provider = createDeepSeekFetchProvider(fetchDeps({
    options: makeOptions({ maxRetries: 0 }),
    fetchImpl,
    lookupFallback: (id) => (id === 'http'
      ? { id, available: () => true, fetch: async () => { usedProvider = id; return localResult } }
      : undefined),
  }))
  const result = await provider.fetch({ url: 'https://example.com/article' })
  assert.equal(usedProvider, 'http')
  assert.deepEqual(result, localResult)
})

test('fetch keeps cancellation instead of falling back', async () => {
  const controller = new AbortController()
  controller.abort(new Error('stop'))
  let fallbackCalls = 0
  const provider = createDeepSeekFetchProvider(fetchDeps({
    options: makeOptions(),
    fetchImpl: async () => jsonResponse(200, {}),
    lookupFallback: () => ({ id: 'http', available: () => true, fetch: async () => { fallbackCalls += 1 } }),
  }))
  await assert.rejects(provider.fetch({ url: 'https://example.com' }, controller.signal), (error) => {
    assert.equal(error.code, 'WEB_ABORTED')
    return true
  })
  assert.equal(fallbackCalls, 0)
})

test('fetch rejects an empty request URL', async () => {
  const provider = createDeepSeekFetchProvider(fetchDeps({
    options: makeOptions({ fetchFallback: false }),
    fetchImpl: async () => jsonResponse(200, {}),
  }))
  await assert.rejects(provider.fetch({}), (error) => {
    assert.equal(error.code, 'WEB_INVALID_URL')
    return true
  })
})
