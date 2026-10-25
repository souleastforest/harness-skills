/**
 * Pure, dependency-free vocabulary for the DeepSeek Search / Fetch tools API.
 *
 * This module deliberately imports nothing so it can be unit-tested with a bare
 * `node --test` outside the Harness process. Everything here is either a
 * constant, a request/response mapping, or an error-classification predicate;
 * the HTTP transport and the Harness integration live in `providers.js` and
 * `index.js` respectively.
 *
 * API reference: https://api-docs.deepseek.com/zh-cn/guides/search/
 */

/** Search provider id registered into `ctx.web` (one id per capability registry). */
export const SEARCH_PROVIDER_ID = 'deepseek-tools'

/** Fetch provider id registered into `ctx.web` (separate registry, same id is fine). */
export const FETCH_PROVIDER_ID = 'deepseek-tools'

/** Search endpoint path, appended to the configured base URL. */
export const SEARCH_PATH = '/v1/tools/search'

/** Fetch endpoint path, appended to the configured base URL. */
export const FETCH_PATH = '/v1/tools/fetch'

/** Official API origin. The guide publishes `https://api.deepseek.com` as the base URL. */
export const DEFAULT_BASE_URL = 'https://api.deepseek.com'

/** Environment override for the tools base URL, independent of the chat adapter's endpoint. */
export const BASE_URL_ENV = 'DEEPSEEK_TOOLS_BASE_URL'

/** Attribution header sent on every request (a product agent, never a browser disguise). */
export const USER_AGENT = 'deepseek-harness/0.0.1 dsh-deepseek-web-tools/1.0.0'

/** Published `count` bounds: 1–50 results per request. */
export const MIN_COUNT = 1
export const MAX_COUNT = 50

/** Published `query` bound: 1–128 Unicode characters. */
export const MAX_QUERY_CHARS = 128

/** Default cap applied to a long `summary` when it is used as the source snippet. */
export const DEFAULT_SNIPPET_CHARS = 600

/** Default cap on fetched Markdown characters, aligned with the web tool's output budget. */
export const DEFAULT_FETCH_MAX_CONTENT_CHARS = 100_000

/** HTTP statuses the guide says are worth retrying after a wait (5xx is added below). */
const RETRYABLE_STATUSES = new Set([408, 429])

/** Longest wait a single retry delay may produce, keeping one search well inside its tool budget. */
const MAX_RETRY_DELAY_MS = 3_000

/**
 * Normalize an operator-supplied base URL into `origin [+ path prefix]`.
 * A base URL must be absolute HTTP(S) without embedded credentials; a path
 * prefix is preserved so gateway deployments that mount the API under a
 * prefix keep working. Trailing slashes are dropped so path joining is exact.
 *
 * @param {unknown} value - candidate base URL.
 * @returns {string | undefined} the normalized base URL, or `undefined` when unusable.
 */
export function normalizeBaseURL(value) {
  if (typeof value !== 'string' || value.trim().length === 0) return undefined
  let url
  try {
    url = new URL(value.trim())
  } catch {
    return undefined
  }
  if (url.protocol !== 'https:' && url.protocol !== 'http:') return undefined
  if (url.username.length > 0 || url.password.length > 0) return undefined
  if (url.search.length > 0 || url.hash.length > 0) return undefined
  const prefix = url.pathname.replace(/\/+$/u, '')
  return `${url.origin}${prefix}`
}

/**
 * Resolve the effective base URL from the literal config, then the environment,
 * then the official default. Throws on an explicitly supplied garbage value so a
 * typo fails at activation rather than at the first search.
 *
 * @param {string | undefined} configured - the row's `baseURL` (may be absent).
 * @param {string | undefined} fromEnvironment - `$DEEPSEEK_TOOLS_BASE_URL`.
 * @returns {string} the normalized base URL.
 * @throws {Error} when an explicitly supplied value is not an absolute http(s) URL.
 */
export function resolveBaseURL(configured, fromEnvironment) {
  const literal = typeof configured === 'string' && configured.trim().length > 0 ? configured : undefined
  const environment = typeof fromEnvironment === 'string' && fromEnvironment.trim().length > 0 ? fromEnvironment : undefined
  const chosen = literal ?? environment
  if (chosen === undefined) return DEFAULT_BASE_URL
  const normalized = normalizeBaseURL(chosen)
  if (normalized === undefined) {
    throw new Error(
      `web-deepseek-tools: baseURL ${JSON.stringify(chosen)} is not an absolute http(s) URL without credentials`,
    )
  }
  return normalized
}

/**
 * Join the base URL and a tools path.
 * @param {string} baseURL - normalized base URL from {@link resolveBaseURL}.
 * @param {string} path - `SEARCH_PATH` or `FETCH_PATH`.
 * @returns {string} the absolute endpoint.
 */
export function endpointFor(baseURL, path) {
  return `${baseURL}${path}`
}

/**
 * Clamp a source count into the API's published 1–50 range.
 * @param {number} value - requested count (from `request.maxResults` or config).
 * @returns {number} an integer within bounds.
 */
export function clampCount(value) {
  const numeric = typeof value === 'number' && Number.isFinite(value) ? Math.trunc(value) : MAX_COUNT
  return Math.min(MAX_COUNT, Math.max(MIN_COUNT, numeric))
}

/**
 * Trim a query and enforce the API's length bound.
 * @param {unknown} query - raw query.
 * @returns {string} the query to send (possibly truncated to {@link MAX_QUERY_CHARS}).
 * @throws {Error} when the query is empty after trimming.
 */
export function prepareQuery(query) {
  const text = typeof query === 'string' ? query.trim() : ''
  if (text.length === 0) throw new Error('a web search query must be a non-empty string')
  return text.length > MAX_QUERY_CHARS ? text.slice(0, MAX_QUERY_CHARS) : text
}

/**
 * Build the Search request body. Unknown-to-this-build fields are simply not
 * sent; the API ignores unknown fields anyway.
 *
 * @param {string} query - prepared query text.
 * @param {number} count - clamped source count.
 * @param {{ needContent?: boolean, localization?: string }} [options] - optional controls.
 * @returns {Record<string, unknown>} the JSON request body.
 */
export function buildSearchBody(query, count, options = {}) {
  const body = { query, count }
  if (options.needContent === true) body.need_content = true
  if (typeof options.localization === 'string' && options.localization.trim().length > 0) {
    body.localization = options.localization.trim()
  }
  return body
}

/** @returns {string | undefined} a trimmed non-empty string, else undefined. */
function pickString(value) {
  if (typeof value !== 'string') return undefined
  const trimmed = value.trim()
  return trimmed.length > 0 ? trimmed : undefined
}

/**
 * Clip a string to `max` characters, appending an ellipsis when clipped.
 * @param {string} value - the text.
 * @param {number} max - character budget.
 * @returns {string} the clipped text.
 */
export function truncateText(value, max) {
  if (value.length <= max) return value
  return `${value.slice(0, Math.max(0, max - 1))}…`
}

/**
 * Map one Search response into the web seam's `WebSearchResult`: deduped
 * sources with `url`, `title`, `snippet`, `publishedAt`. `snippet` prefers the
 * short snippet, then the long summary (clipped for a single model-facing
 * line), then body content when the request asked for it. `truncated` is
 * always false here: the seam owns the `maxResults` cap and sets it itself.
 *
 * @param {unknown} json - the parsed response body.
 * @param {{ snippetChars?: number }} [options] - snippet clipping budget.
 * @returns {{ sources: Array<{ url: string, title?: string, snippet?: string, publishedAt?: string }>, truncated: boolean }}
 * @throws {Error} when the body does not carry the documented `results` array.
 */
export function mapSearchResponse(json, options = {}) {
  if (json === null || typeof json !== 'object' || !Array.isArray(json.results)) {
    throw new Error('the response is missing the documented "results" array')
  }
  const snippetChars = Number.isInteger(options.snippetChars) && options.snippetChars > 0
    ? options.snippetChars
    : DEFAULT_SNIPPET_CHARS
  const seen = new Set()
  const sources = []
  for (const raw of json.results) {
    if (raw === null || typeof raw !== 'object') continue
    const url = pickString(raw.url)
    if (url === undefined || seen.has(url)) continue
    seen.add(url)
    const source = { url }
    const title = pickString(raw.title)
    if (title !== undefined) source.title = title
    // The short snippet is already presentation-sized; the long summary and the
    // optional body content are only fallbacks, clipped to one model-facing line.
    const shortSnippet = pickString(raw.snippet)
    const longText = pickString(raw.summary) ?? pickString(raw.content)
    const snippet = shortSnippet ?? (longText !== undefined ? truncateText(longText, snippetChars) : undefined)
    if (snippet !== undefined) source.snippet = snippet
    const publishedAt = pickString(raw.date)
    if (publishedAt !== undefined) source.publishedAt = publishedAt
    sources.push(source)
  }
  return { sources, truncated: false }
}

/**
 * Map one Fetch response into the web seam's `WebFetchResult`. The API returns
 * Markdown in `content`; the tool renders a `text` body verbatim, so the
 * Markdown reaches the model unchanged. A non-empty `title` is prepended as a
 * heading whenever the content does not already start with one, so the page
 * keeps its name after the fetch card's provenance header.
 *
 * @param {unknown} json - the parsed response body.
 * @param {string} url - the requested URL (the API does not report a final URL).
 * @param {number} maxContentChars - character cap on the returned Markdown.
 * @returns {{ url: string, statusCode: number, body: { kind: 'text', content: string }, truncated: boolean }}
 * @throws {Error} when the body carries no page content.
 */
export function mapFetchResponse(json, url, maxContentChars) {
  if (json === null || typeof json !== 'object') throw new Error('the response body is not an object')
  const content = typeof json.content === 'string' ? json.content : ''
  if (content.trim().length === 0) throw new Error('the response carries no page content')
  const title = pickString(json.title)
  let body = title !== undefined && !/^\s*#/u.test(content) ? `# ${title}\n\n${content}` : content
  let truncated = false
  const cap = Number.isInteger(maxContentChars) && maxContentChars > 0
    ? maxContentChars
    : DEFAULT_FETCH_MAX_CONTENT_CHARS
  if (body.length > cap) {
    body = body.slice(0, cap)
    truncated = true
  }
  return { url, statusCode: 200, body: { kind: 'text', content: body }, truncated }
}

/**
 * Extract the human-readable detail from a documented OpenAI-compatible error body.
 * @param {unknown} json - the parsed error body (or raw text).
 * @returns {string} the detail, or an empty string when none is present.
 */
export function errorDetail(json) {
  if (typeof json === 'string') return json.trim()
  if (json === null || typeof json !== 'object') return ''
  const candidates = [
    typeof json.error === 'string' ? json.error : undefined,
    json.error !== null && typeof json.error === 'object' ? json.error.message : undefined,
    json.message,
    json.detail,
  ]
  for (const candidate of candidates) {
    if (typeof candidate === 'string' && candidate.trim().length > 0) return candidate.trim()
  }
  return ''
}

/**
 * Whether an HTTP status is safe to retry after a short wait (the guide names
 * 408 and 429, and every 5xx is a transient server-side failure).
 * @param {number} status - the HTTP status.
 * @returns {boolean} true when a retry is advised.
 */
export function isRetryableStatus(status) {
  return RETRYABLE_STATUSES.has(status) || (Number.isInteger(status) && status >= 500 && status <= 599)
}

/**
 * Backoff for retry `attempt` (0-based), with jitter to avoid synchronized
 * parallel queries (one `web_search` call runs up to four at once) all retrying
 * in lockstep against the account-level QPS limit.
 *
 * @param {number} attempt - 0-based retry index.
 * @param {() => number} [random] - jitter source, injectable for tests.
 * @returns {number} milliseconds to wait before the retry.
 */
export function retryDelayMs(attempt, random = Math.random) {
  const exponential = 400 * 2 ** Math.max(0, attempt)
  return Math.min(MAX_RETRY_DELAY_MS, Math.round(exponential + random() * 200))
}

/**
 * Whether a provider failure may be handed to the configured fallback
 * provider. Caller cancellation is the one exception: a cancel must surface as
 * a cancel, never as a successful fallback that ignores the user's stop.
 *
 * @param {unknown} error - the failure thrown by the primary path.
 * @returns {boolean} true when fallback is allowed.
 */
export function shouldFallback(error) {
  if (error === undefined || error === null) return false
  if (error.code === 'WEB_ABORTED') return false
  return true
}
