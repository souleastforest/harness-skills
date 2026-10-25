/**
 * DeepSeek Search & Fetch tools providers for the `ctx.web` seam.
 *
 * The shipped `@deepseek-ai/dsh-web-search-deepseek` provider predates DeepSeek's
 * dedicated retrieval endpoints: it runs a full Anthropic-compatible Messages
 * turn with the native `web_search` server tool, because DeepSeek published no
 * standalone search API at the time. The guide this bundle implements
 * (https://api-docs.deepseek.com/zh-cn/guides/search/) now publishes two plain
 * JSON endpoints on the same Bearer auth as chat:
 *
 *   POST /v1/tools/search   structured results, ~0.4 s
 *   POST /v1/tools/fetch    one page as Markdown
 *
 * This row registers providers for both capabilities under the `deepseek-tools`
 * id and keeps the previous backends mounted as the fallback chain:
 *   - search falls back to `deepseek-official` (the Messages adapter),
 *   - fetch falls back to `http` (the anonymous local fetcher).
 *
 * Reverting is a bundle-level operation: disable or remove this bundle and the
 * row override in `cordis.patch.yml` disappears with it, restoring the base
 * composition (`deepseek-official` + `http`) exactly. Nothing in the profile's
 * own patch layer is touched.
 *
 * The module imports nothing from `@deepseek-ai/*` on purpose: a workspace bundle
 * installed by `plugin_manager` is a `link:` dependency outside the profile's
 * module graph, and bare Harness specifiers do not resolve from there (see
 * `lib/web-error.js`). Configuration is therefore resolved and validated here at
 * activation instead of by a schemastery `Config` schema in the settings UI.
 *
 * @module @local/dsh-deepseek-web-tools
 */

import { WebError } from './lib/web-error.js'
import { createDeepSeekFetchProvider, createDeepSeekSearchProvider } from './lib/providers.js'
import {
  BASE_URL_ENV,
  DEFAULT_FETCH_MAX_CONTENT_CHARS,
  DEFAULT_SNIPPET_CHARS,
  FETCH_PROVIDER_ID,
  SEARCH_PROVIDER_ID,
  resolveBaseURL,
} from './lib/core.js'

/** Cordis plugin name used by loader diagnostics. */
export const name = 'web-deepseek-tools'

/** The web seam this row contributes providers to. */
export const inject = ['web']

/** Provider route id `dsh-llm-deepseek-account` registers; `request/context` events record it per Session. */
const ACCOUNT_PROVIDER_ID = 'deepseek-account'

/** Maximum Node timer delay; a timeout above it is coerced silently, so reject it at activation. */
const MAX_NODE_TIMER_DELAY_MS = 2_147_483_647

/** Upper bound on retries, so a typo cannot turn one search into a long retry storm. */
const MAX_RETRIES_LIMIT = 10

/**
 * Every accepted row-config key with its default. These are the documented
 * knobs; `resolveConfig` rejects any other key so a typo fails activation loudly
 * instead of silently doing nothing (there is no settings-ui schema here).
 */
export const DEFAULT_CONFIG = Object.freeze({
  /** Literal API key. Prefer `apiKeyEnv` so the secret stays out of configuration files. */
  apiKey: undefined,
  /** Credential reference resolved through `ctx.credentials` (else the launching environment) per request. */
  apiKeyEnv: 'DEEPSEEK_API_KEY',
  /** Tools API origin. Falls back to `$DEEPSEEK_TOOLS_BASE_URL`, then `https://api.deepseek.com`. */
  baseURL: undefined,
  /** Per-attempt deadline in milliseconds. Kept well under the `web_search` tool budget (60 s). */
  timeoutMs: 15_000,
  /** Retries for retryable HTTP statuses (408, 429, 5xx) and transport errors. Timeouts are never retried. */
  maxRetries: 1,
  /** Fallback count when a caller passes no `maxResults`; a caller's value always wins (clamped 1–50). */
  searchCount: 10,
  /** Ask the API to return page bodies in `content` too. Off by default: snippets and summaries are enough for `web_search`. */
  searchNeedContent: false,
  /** Optional `localization` IP hint forwarded verbatim to the Search endpoint. */
  searchLocalization: undefined,
  /** When the Search endpoint fails, retry through the fallback search provider registered in `ctx.web`. */
  searchFallback: true,
  /** Search fallback provider id; the shipped Messages-based adapter by default. */
  searchFallbackProvider: 'deepseek-official',
  /** When the Fetch endpoint fails, retry through the fallback fetch provider registered in `ctx.web`. */
  fetchFallback: true,
  /** Fetch fallback provider id; the shipped anonymous HTTP fetcher by default. */
  fetchFallbackProvider: 'http',
  /** Character cap on fetched Markdown; the tool applies its own output cap as well. */
  fetchMaxContentChars: DEFAULT_FETCH_MAX_CONTENT_CHARS,
  /** Clip budget for a long `summary` used as a source snippet. */
  snippetChars: DEFAULT_SNIPPET_CHARS,
})

/**
 * Resolve and validate one row config into the fully-defaulted options object.
 *
 * @param {unknown} raw - the row's `config` as the Loader supplied it (may be absent).
 * @returns {typeof DEFAULT_CONFIG} the resolved options.
 * @throws {Error} naming the offending key when a value is unknown, mistyped, or out of range.
 */
export function resolveConfig(raw) {
  if (raw === undefined || raw === null) raw = {}
  if (typeof raw !== 'object' || Array.isArray(raw)) throw new Error('web-deepseek-tools: the row config must be a mapping')
  const unknown = Object.keys(raw).filter((key) => !(key in DEFAULT_CONFIG))
  if (unknown.length > 0) {
    throw new Error(
      `web-deepseek-tools: unknown config key(s) ${unknown.map((key) => JSON.stringify(key)).join(', ')}; accepted keys: ${Object.keys(DEFAULT_CONFIG).join(', ')}`,
    )
  }
  const config = { ...DEFAULT_CONFIG }
  for (const key of Object.keys(raw)) if (raw[key] !== undefined) config[key] = raw[key]

  assertOptionalNonEmptyString('apiKey', config.apiKey)
  assertNonEmptyString('apiKeyEnv', config.apiKeyEnv)
  assertOptionalNonEmptyString('baseURL', config.baseURL)
  assertOptionalNonEmptyString('searchLocalization', config.searchLocalization)
  assertTimeoutMs(config.timeoutMs)
  assertIntegerInRange('maxRetries', config.maxRetries, 0, MAX_RETRIES_LIMIT)
  assertIntegerInRange('searchCount', config.searchCount, 1, 50)
  assertPositiveInteger('fetchMaxContentChars', config.fetchMaxContentChars)
  assertPositiveInteger('snippetChars', config.snippetChars)
  assertBoolean('searchNeedContent', config.searchNeedContent)
  assertBoolean('searchFallback', config.searchFallback)
  assertBoolean('fetchFallback', config.fetchFallback)
  assertFallbackProvider('searchFallbackProvider', config.searchFallback, config.searchFallbackProvider)
  assertFallbackProvider('fetchFallbackProvider', config.fetchFallback, config.fetchFallbackProvider)
  return config
}

/**
 * Register both DeepSeek tools providers into `ctx.web`.
 *
 * @param {import('@deepseek-ai/cordis').Context} ctx - the plugin context; `web` is injected.
 * @param {unknown} config - the row config; resolved and validated here.
 * @throws {Error} when the config is invalid or the base URL cannot be resolved.
 */
export function apply(ctx, config) {
  const resolved = resolveConfig(config)
  const baseURL = resolveBaseURL(resolved.baseURL, process.env[BASE_URL_ENV])

  const createError = (message, code, options) => new WebError(message, code, options)
  const record = (type, payload) => recordEvent(ctx, type, payload)
  const fetchImpl = (input, init) => globalThis.fetch(input, init)
  const resolveOptions = () => ({
    ...resolved,
    baseURL,
    resolveApiKey: () => resolveApiKey(ctx, resolved),
    resolveAccountToken: (endpoint) => resolveAccountToken(ctx, endpoint),
  })

  ctx.web.registerSearchProvider(createDeepSeekSearchProvider({
    id: SEARCH_PROVIDER_ID,
    resolveOptions,
    createError,
    fetchImpl,
    lookupFallback: (providerId) => ctx.web.searchProviders?.get?.(providerId),
    record,
  }))
  ctx.web.registerFetchProvider(createDeepSeekFetchProvider({
    id: FETCH_PROVIDER_ID,
    resolveOptions,
    createError,
    fetchImpl,
    lookupFallback: (providerId) => ctx.web.fetchProviders?.get?.(providerId),
    record,
  }))
}

/**
 * Resolve one API key: the literal config, then `ctx.credentials`, then the
 * launching environment. The credentials service already layers the inherited
 * environment and `.env` fallbacks underneath its managed store, so it is
 * authoritative whenever it exists; the environment read is only the
 * no-credentials-service fallback.
 *
 * @param {import('@deepseek-ai/cordis').Context} ctx - plugin context.
 * @param {{ apiKey?: string, apiKeyEnv: string }} config - the resolved row config.
 * @returns {Promise<string | undefined>} the key, or undefined when none resolves.
 */
async function resolveApiKey(ctx, config) {
  if (typeof config.apiKey === 'string' && config.apiKey.length > 0) return config.apiKey
  const envName = config.apiKeyEnv
  const credentials = ctx.get('credentials')
  if (credentials !== undefined && typeof credentials.resolve === 'function') {
    const resolved = await credentials.resolve(envName)
    return typeof resolved?.value === 'string' && resolved.value.length > 0 ? resolved.value : undefined
  }
  const ambient = process.env[envName]
  return typeof ambient === 'string' && ambient.length > 0 ? ambient : undefined
}

/**
 * Resolve the DeepSeek account ticket for a search/fetch, but only for a
 * session currently routed through the `deepseek-account` LLM provider. The
 * account service itself returns undefined for destinations outside its
 * configured inference origin, so this never leaks the ticket to a third-party
 * endpoint.
 *
 * @param {import('@deepseek-ai/cordis').Context} ctx - plugin context.
 * @param {string} endpoint - the tools endpoint about to be called.
 * @returns {Promise<string | undefined>} the ticket, or undefined when this is not an account session.
 */
async function resolveAccountToken(ctx, endpoint) {
  const initiator = ctx.get('agents')?.currentInitiator?.()
  const requestContext = initiator?.session?.requestContext?.()
  if (requestContext?.provider !== ACCOUNT_PROVIDER_ID) return undefined
  const account = ctx.get('deepseekAccount')
  if (account === undefined || typeof account.resolveToken !== 'function') return undefined
  return account.resolveToken(endpoint)
}

/**
 * Append a logging-only session event (request body, fallback decisions). Never
 * throws: a session that cannot record must not fail the underlying search.
 *
 * @param {import('@deepseek-ai/cordis').Context} ctx - plugin context.
 * @param {string} type - the event type.
 * @param {unknown} payload - JSON-serializable payload without credentials.
 */
function recordEvent(ctx, type, payload) {
  try {
    ctx.get('agents')?.currentInitiator?.()?.session?.append?.(type, payload)
  } catch {
    /* logging is best-effort by contract */
  }
}

/* ───────────────────────── config validators ───────────────────────── */

/** Reject a key whose present value is not a non-empty string. */
function assertOptionalNonEmptyString(field, value) {
  if (value === undefined) return
  assertNonEmptyString(field, value)
}

/** Reject a value that is not a non-empty string. */
function assertNonEmptyString(field, value) {
  if (typeof value !== 'string' || value.length === 0) throw new Error(`web-deepseek-tools: ${field} must be a non-empty string`)
}

/** Reject a boolean flag that is not actually a boolean (a quoted "true" must not pass silently). */
function assertBoolean(field, value) {
  if (typeof value !== 'boolean') throw new Error(`web-deepseek-tools: ${field} must be true or false`)
}

/** Reject a positive integer resource limit at activation. */
function assertPositiveInteger(field, value) {
  if (!Number.isInteger(value) || value <= 0) throw new Error(`web-deepseek-tools: ${field} must be a positive integer`)
}

/** Reject an integer outside the accepted range at activation. */
function assertIntegerInRange(field, value, min, max) {
  if (!Number.isInteger(value) || value < min || value > max) {
    throw new Error(`web-deepseek-tools: ${field} must be an integer between ${min} and ${max}`)
  }
}

/** Reject a timeout outside the range Node timers can express. */
function assertTimeoutMs(value) {
  assertPositiveInteger('timeoutMs', value)
  if (value > MAX_NODE_TIMER_DELAY_MS) throw new Error(`web-deepseek-tools: timeoutMs must be no greater than ${MAX_NODE_TIMER_DELAY_MS}`)
}

/** Require a fallback provider id whenever fallback is enabled. */
function assertFallbackProvider(field, enabled, value) {
  if (enabled !== true) return
  assertNonEmptyString(field, value)
}

export { SEARCH_PROVIDER_ID, FETCH_PROVIDER_ID }
