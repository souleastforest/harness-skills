import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  MAX_COUNT,
  MAX_QUERY_CHARS,
  buildSearchBody,
  clampCount,
  endpointFor,
  errorDetail,
  isRetryableStatus,
  mapFetchResponse,
  mapSearchResponse,
  normalizeBaseURL,
  prepareQuery,
  resolveBaseURL,
  retryDelayMs,
  shouldFallback,
  truncateText,
} from '../lib/core.js'

test('normalizeBaseURL keeps origin and optional path prefix, drops trailing slashes', () => {
  assert.equal(normalizeBaseURL('https://api.deepseek.com'), 'https://api.deepseek.com')
  assert.equal(normalizeBaseURL('api.deepseek.com'), undefined)
  assert.equal(normalizeBaseURL('  https://gw.internal/deepseek/v1/  '), 'https://gw.internal/deepseek/v1')
  assert.equal(normalizeBaseURL('http://127.0.0.1:8080'), 'http://127.0.0.1:8080')
})

test('normalizeBaseURL rejects non-http schemes, credentials, query and hash', () => {
  assert.equal(normalizeBaseURL('ftp://api.deepseek.com'), undefined)
  assert.equal(normalizeBaseURL('https://user:pass@api.deepseek.com'), undefined)
  assert.equal(normalizeBaseURL('https://api.deepseek.com?x=1'), undefined)
  assert.equal(normalizeBaseURL('https://api.deepseek.com#x'), undefined)
  assert.equal(normalizeBaseURL(''), undefined)
  assert.equal(normalizeBaseURL(undefined), undefined)
})

test('resolveBaseURL prefers the literal, then the environment, then the official origin', () => {
  assert.equal(resolveBaseURL(undefined, undefined), 'https://api.deepseek.com')
  assert.equal(resolveBaseURL(undefined, 'https://gw.internal'), 'https://gw.internal')
  assert.equal(resolveBaseURL('https://literal.internal', 'https://gw.internal'), 'https://literal.internal')
  assert.equal(resolveBaseURL('', '  '), 'https://api.deepseek.com')
})

test('resolveBaseURL fails loud on an explicitly supplied invalid value', () => {
  assert.throws(() => resolveBaseURL('not a url', undefined), /not an absolute http\(s\) URL/u)
  assert.throws(() => resolveBaseURL(undefined, 'ftp://x'), /not an absolute http\(s\) URL/u)
})

test('endpointFor joins without duplicate slashes', () => {
  assert.equal(endpointFor('https://api.deepseek.com', '/v1/tools/search'), 'https://api.deepseek.com/v1/tools/search')
  assert.equal(endpointFor('https://gw/x', '/v1/tools/fetch'), 'https://gw/x/v1/tools/fetch')
})

test('clampCount stays inside the published range', () => {
  assert.equal(clampCount(1), 1)
  assert.equal(clampCount(8), 8)
  assert.equal(clampCount(0), 1)
  assert.equal(clampCount(999), MAX_COUNT)
  assert.equal(clampCount(Number.NaN), MAX_COUNT)
  assert.equal(clampCount(undefined), MAX_COUNT)
})

test('prepareQuery trims, truncates to 128 characters, and rejects blank input', () => {
  assert.equal(prepareQuery('  DeepSeek 发布  '), 'DeepSeek 发布')
  assert.equal(prepareQuery('x'.repeat(200)).length, MAX_QUERY_CHARS)
  assert.throws(() => prepareQuery('   '), /non-empty/u)
  assert.throws(() => prepareQuery(42), /non-empty/u)
})

test('buildSearchBody only sends options that were requested', () => {
  assert.deepEqual(buildSearchBody('q', 5, {}), { query: 'q', count: 5 })
  assert.deepEqual(buildSearchBody('q', 5, { needContent: true }), { query: 'q', count: 5, need_content: true })
  assert.deepEqual(buildSearchBody('q', 5, { localization: ' 1.2.3.4 ' }), { query: 'q', count: 5, localization: '1.2.3.4' })
})

test('mapSearchResponse maps, dedupes, and prefers the short snippet', () => {
  const result = mapSearchResponse({
    results: [
      {
        rerank_score: 2.4,
        title: '  Title A  ',
        url: 'https://example.com/a',
        snippet: 'short',
        summary: 'long summary',
        content: 'body',
        date: '2026-06-01T00:00:00+00:00',
        authority: 3,
      },
      { url: 'https://example.com/a', title: 'duplicate' },
      { title: 'no url' },
      { url: 'https://example.com/b', summary: 'sum'.repeat(400) },
    ],
  })
  assert.equal(result.truncated, false)
  assert.equal(result.sources.length, 2)
  assert.deepEqual(result.sources[0], {
    url: 'https://example.com/a',
    title: 'Title A',
    snippet: 'short',
    publishedAt: '2026-06-01T00:00:00+00:00',
  })
  assert.equal(result.sources[1].url, 'https://example.com/b')
  assert.ok(result.sources[1].snippet.endsWith('…'))
  assert.ok(result.sources[1].snippet.length <= 600)
})

test('mapSearchResponse accepts an empty results array and rejects a missing one', () => {
  assert.deepEqual(mapSearchResponse({ results: [] }), { sources: [], truncated: false })
  assert.throws(() => mapSearchResponse({}), /"results" array/u)
  assert.throws(() => mapSearchResponse(null), /"results" array/u)
})

test('mapFetchResponse returns a text body and prepends the title as a heading', () => {
  const result = mapFetchResponse({ title: 'Example Domain', content: 'Hello **world**.' }, 'https://example.com', 10_000)
  assert.deepEqual(result, {
    url: 'https://example.com',
    statusCode: 200,
    body: { kind: 'text', content: '# Example Domain\n\nHello **world**.' },
    truncated: false,
  })
})

test('mapFetchResponse does not duplicate an existing heading and caps long content', () => {
  const withHeading = mapFetchResponse({ title: 'T', content: '# Already here' }, 'https://e.com', 100)
  assert.equal(withHeading.body.content, '# Already here')
  const capped = mapFetchResponse({ title: 'T', content: 'x'.repeat(50) }, 'https://e.com', 10)
  assert.equal(capped.truncated, true)
  assert.equal(capped.body.content.length, 10)
})

test('mapFetchResponse rejects an empty page body', () => {
  assert.throws(() => mapFetchResponse({ title: 'T', content: '   ' }, 'https://e.com', 100), /no page content/u)
  assert.throws(() => mapFetchResponse(null, 'https://e.com', 100), /not an object/u)
})

test('errorDetail reads the documented OpenAI-compatible error shape', () => {
  assert.equal(errorDetail({ error: { message: 'bad count', type: 'invalid_request_error', param: null } }), 'bad count')
  assert.equal(errorDetail({ error: 'plain' }), 'plain')
  assert.equal(errorDetail({ message: 'm' }), 'm')
  assert.equal(errorDetail({ error: { param: null } }), '')
  assert.equal(errorDetail('raw text'), 'raw text')
})

test('isRetryableStatus names 408, 429 and every 5xx', () => {
  for (const status of [408, 429, 500, 502, 503, 504]) assert.equal(isRetryableStatus(status), true, String(status))
  for (const status of [400, 401, 402, 403, 404, 422]) assert.equal(isRetryableStatus(status), false, String(status))
})

test('retryDelayMs is exponential and bounded', () => {
  assert.equal(retryDelayMs(0, () => 0), 400)
  assert.equal(retryDelayMs(1, () => 0), 800)
  assert.equal(retryDelayMs(5, () => 0), 3000)
  assert.ok(retryDelayMs(0, () => 1) <= 600)
})

test('shouldFallback refuses cancellation only', () => {
  assert.equal(shouldFallback({ code: 'WEB_ABORTED' }), false)
  assert.equal(shouldFallback({ code: 'WEB_PROVIDER_ERROR' }), true)
  assert.equal(shouldFallback({ code: 'WEB_PROVIDER_CREDENTIAL_MISSING' }), true)
  assert.equal(shouldFallback(undefined), false)
})

test('truncateText appends an ellipsis only when clipped', () => {
  assert.equal(truncateText('abc', 10), 'abc')
  assert.equal(truncateText('abcdef', 4), 'abc…')
  assert.equal(truncateText('abcdef', 0), '…')
})
