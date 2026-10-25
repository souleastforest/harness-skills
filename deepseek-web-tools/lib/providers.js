/**
 * The two `ctx.web` providers over the DeepSeek Search / Fetch tools API.
 *
 * This module is dependency-injected on purpose: it imports nothing, so the
 * whole transport — auth resolution, retry, result mapping, and the fallback
 * chain — can be exercised by `node --test` with a stub `fetchImpl` and a stub
 * error factory. `index.js` supplies the Harness-specific pieces (`WebError`,
 * the credential/account resolvers, the provider registry lookup, and the
 * session event recorder).
 *
 * The fallback contract, in one line: a primary failure that is not caller
 * cancellation is handed to the configured fallback provider registered in the
 * *same* capability registry; if that provider is absent, unusable, or fails
 * too, the caller sees one error carrying both failures. Nothing here mutates
 * the seam's selection state, so switching backends stays a configuration
 * decision while a backend outage stays invisible to the agent.
 */

import {
  DEFAULT_SNIPPET_CHARS,
  FETCH_PATH,
  SEARCH_PATH,
  USER_AGENT,
  buildSearchBody,
  clampCount,
  endpointFor,
  errorDetail,
  isRetryableStatus,
  mapFetchResponse,
  mapSearchResponse,
  prepareQuery,
  retryDelayMs,
  shouldFallback,
  truncateText,
} from './core.js'

/** Provider id used by the DeepSeek account LLM route; searches from these sessions prefer the account token. */
const ACCOUNT_PROVIDER_ID = 'deepseek-account'

/** Session event types appended by this provider (logging only; a failure to record never fails a request). */
const EVENT_REQUEST = 'web/deepseek-tools-request'
const EVENT_FALLBACK = 'web/deepseek-tools-fallback'
const EVENT_FALLBACK_SKIPPED = 'web/deepseek-tools-fallback-skipped'
const EVENT_ACCOUNT_ERROR = 'web/deepseek-tools-account-error'

/**
 * Create the search provider.
 *
 * @param {{
 *   id: string,
 *   resolveOptions: () => Record<string, any>,
 *   createError: (message: string, code: string, options?: { cause?: unknown }) => Error,
 *   fetchImpl: (input: string, init: Record<string, unknown>) => Promise<Response>,
 *   lookupFallback: (providerId: string) => { available?: () => boolean, search?: Function } | undefined,
 *   record: (type: string, payload: unknown) => void,
 * }} deps - the injected collaborators.
 * @returns {{ id: string, available: () => boolean, search: (request: any, signal?: AbortSignal) => Promise<any> }}
 */
export function createDeepSeekSearchProvider(deps) {
  const { id, resolveOptions, createError, fetchImpl, lookupFallback, record } = deps
  return {
    id,
    available() {
      return canDispatch(resolveOptions())
    },
    async search(request, signal) {
      const options = resolveOptions()
      throwIfAborted(createError, signal)
      const endpoint = endpointForOptions(options, SEARCH_PATH)
      let body
      try {
        const query = prepareQuery(request?.query)
        body = buildSearchBody(query, clampCount(request?.maxResults ?? options.searchCount), {
          needContent: options.searchNeedContent,
          localization: options.searchLocalization,
        })
      } catch (error) {
        throw createError(`DeepSeek search rejected the request before dispatch: ${messageOf(error)}`, 'WEB_PROVIDER_ERROR', { cause: error })
      }
      let primaryError
      try {
        const json = await postJSON({ operation: 'search', endpoint, body, options, signal, createError, fetchImpl, record })
        try {
          return mapSearchResponse(json, { snippetChars: options.snippetChars ?? DEFAULT_SNIPPET_CHARS })
        } catch (error) {
          throw createError(`DeepSeek search returned an unusable response: ${messageOf(error)}`, 'WEB_PROVIDER_ERROR', { cause: error })
        }
      } catch (error) {
        primaryError = enrichEndpointError(createError, error, 'search', endpoint)
      }
      return runFallback({
        operation: 'search',
        endpoint,
        request,
        signal,
        providerId: options.searchFallbackProvider,
        enabled: options.searchFallback,
        selfId: id,
        primaryError,
        lookupFallback,
        record,
        createError,
        invoke: (provider) => provider.search(request, signal),
      })
    },
  }
}

/**
 * Create the fetch provider.
 *
 * @param {{
 *   id: string,
 *   resolveOptions: () => Record<string, any>,
 *   createError: (message: string, code: string, options?: { cause?: unknown }) => Error,
 *   fetchImpl: (input: string, init: Record<string, unknown>) => Promise<Response>,
 *   lookupFallback: (providerId: string) => { available?: () => boolean, fetch?: Function } | undefined,
 *   record: (type: string, payload: unknown) => void,
 * }} deps - the injected collaborators.
 * @returns {{ id: string, available: () => boolean, fetch: (request: any, signal?: AbortSignal) => Promise<any> }}
 */
export function createDeepSeekFetchProvider(deps) {
  const { id, resolveOptions, createError, fetchImpl, lookupFallback, record } = deps
  return {
    id,
    available() {
      return canDispatch(resolveOptions())
    },
    async fetch(request, signal) {
      const options = resolveOptions()
      throwIfAborted(createError, signal)
      const endpoint = endpointForOptions(options, FETCH_PATH)
      const url = typeof request?.url === 'string' ? request.url.trim() : ''
      let primaryError
      try {
        if (url.length === 0) throw createError('a web fetch request must carry a URL', 'WEB_INVALID_URL')
        const json = await postJSON({ operation: 'fetch', endpoint, body: { url }, options, signal, createError, fetchImpl, record })
        try {
          return mapFetchResponse(json, url, options.fetchMaxContentChars)
        } catch (error) {
          throw createError(`DeepSeek fetch returned no usable page: ${messageOf(error)}`, 'WEB_PROVIDER_ERROR', { cause: error })
        }
      } catch (error) {
        primaryError = enrichEndpointError(createError, error, 'fetch', endpoint)
      }
      return runFallback({
        operation: 'fetch',
        endpoint,
        request,
        signal,
        providerId: options.fetchFallbackProvider,
        enabled: options.fetchFallback,
        selfId: id,
        primaryError,
        lookupFallback,
        record,
        createError,
        invoke: (provider) => provider.fetch(request, signal),
      })
    },
  }
}

/* ───────────────────────────── transport ───────────────────────────── */

/**
 * POST one JSON request with the DeepSeek auth precedence and the documented
 * retry policy. Auth escalation is part of the same loop: an account-token 401
 * retries once with the stored API key before it becomes a provider failure.
 *
 * @param {{
 *   operation: 'search' | 'fetch',
 *   endpoint: string,
 *   body: Record<string, unknown>,
 *   options: Record<string, any>,
 *   signal: AbortSignal | undefined,
 *   createError: Function,
 *   fetchImpl: Function,
 *   record: Function,
 * }} params - one dispatch.
 * @returns {Promise<any>} the parsed JSON response body.
 */
async function postJSON({ operation, endpoint, body, options, signal, createError, fetchImpl, record }) {
  let auth = await resolveAuth({ options, endpoint, signal, createError, record })
  record?.(EVENT_REQUEST, { operation, endpoint, body })
  let attempt = 0
  for (;;) {
    throwIfAborted(createError, signal)
    const attemptSignal = attemptSignalFor(signal, options.timeoutMs)
    let response
    try {
      response = await fetchImpl(endpoint, {
        method: 'POST',
        redirect: 'error',
        headers: {
          ...auth.headers,
          'content-type': 'application/json',
          accept: 'application/json',
          'user-agent': options.userAgent ?? USER_AGENT,
        },
        body: JSON.stringify(body),
        signal: attemptSignal,
      })
    } catch (error) {
      if (signal?.aborted === true) throw abortedError(createError, signal, error)
      // Only this attempt's own deadline can abort an otherwise-unaborted call.
      // A deadline is not retried: it already consumed the operation's latency
      // budget, and the fallback path is the better next attempt.
      const timedOut = attemptSignal.aborted === true
      if (!timedOut && attempt < options.maxRetries) {
        await delay(retryDelayMs(attempt), signal, createError)
        attempt += 1
        continue
      }
      throw timedOut
        ? createError(
          `DeepSeek tools ${operation} timed out after ${options.timeoutMs} ms on attempt ${attempt + 1}: ${messageOf(error)}`,
          'WEB_PROVIDER_TIMEOUT',
          { cause: error },
        )
        : createError(
          `DeepSeek tools ${operation} request failed after ${attempt + 1} attempt(s): ${messageOf(error)}`,
          'WEB_PROVIDER_ERROR',
          { cause: error },
        )
    }
    if (response.status === 401 && auth.kind === 'account') {
      const apiKey = await resolveApiKey(options, createError)
      if (typeof apiKey === 'string' && apiKey.length > 0) {
        auth = apiKeyAuth(apiKey)
        continue
      }
    }
    if (!response.ok) {
      const status = response.status
      const detail = await readErrorDetail(response)
      if (isRetryableStatus(status) && attempt < options.maxRetries) {
        await delay(retryDelayMs(attempt), signal, createError)
        attempt += 1
        continue
      }
      const error = createError(
        `DeepSeek tools ${operation} failed (HTTP ${status})${detail.length > 0 ? `: ${detail}` : ''}`,
        'WEB_PROVIDER_ERROR',
        { cause: { status, detail } },
      )
      error.httpStatus = status
      throw error
    }
    try {
      return await response.json()
    } catch (error) {
      throw createError(
        `DeepSeek tools ${operation} returned an unprocessable response body: ${messageOf(error)}`,
        'WEB_PROVIDER_ERROR',
        { cause: error },
      )
    }
  }
}

/**
 * Resolve the auth headers for one request. A DeepSeek-account-routed session
 * tries the account ticket first (the same ticket the account LLM adapter
 * sends); everything else uses the API key. Account-token resolution failures
 * are recorded and skipped rather than fatal, so a stale ticket degrades to the
 * API key instead of breaking search.
 *
 * @returns {Promise<{ kind: 'account' | 'api-key', headers: Record<string, string> }>} the auth choice.
 */
async function resolveAuth({ options, endpoint, signal, createError, record }) {
  if (typeof options.resolveAccountToken === 'function') {
    let token
    try {
      token = await options.resolveAccountToken(endpoint)
    } catch (error) {
      token = undefined
      record?.(EVENT_ACCOUNT_ERROR, { message: messageOf(error) })
    }
    if (typeof token === 'string' && token.length > 0) {
      return { kind: 'account', headers: { 'x-dsh-auth-token': token } }
    }
  }
  const apiKey = await resolveApiKey(options, createError)
  return apiKeyAuth(apiKey)
}

/** Build the documented API-key header pair. */
function apiKeyAuth(apiKey) {
  return { kind: 'api-key', headers: { authorization: `Bearer ${apiKey}`, 'x-api-key': apiKey } }
}

/**
 * Resolve one API key: the literal config first, then the injected resolver
 * (credentials service, else the launching environment).
 *
 * @returns {Promise<string | undefined>} the key, or undefined when none resolves.
 */
async function resolveApiKey(options, createError) {
  if (typeof options.apiKey === 'string' && options.apiKey.length > 0) return options.apiKey
  let resolved
  try {
    resolved = await options.resolveApiKey()
  } catch (error) {
    throw createError(
      `DeepSeek Search/Fetch credential resolution failed: ${messageOf(error)}`,
      'WEB_PROVIDER_ERROR',
      { cause: error },
    )
  }
  if (typeof resolved === 'string' && resolved.length > 0) return resolved
  throw createError(
    `DeepSeek Search/Fetch has no API key for "${options.apiKeyEnv ?? 'DEEPSEEK_API_KEY'}"; store it through the credentials service (the Models page writes it), export it in the launching environment, or set a literal "apiKey" in the web-deepseek-tools config. A conversation using a DeepSeek Account model signs in with the account instead.`,
    'WEB_PROVIDER_CREDENTIAL_MISSING',
  )
}

/** Read a documented OpenAI-compatible error body without letting a body failure mask the status. */
async function readErrorDetail(response) {
  let text
  try {
    text = await response.text()
  } catch {
    return ''
  }
  if (typeof text !== 'string' || text.trim().length === 0) return ''
  let parsed
  try {
    parsed = JSON.parse(text)
  } catch {
    return truncateText(text.trim(), 400)
  }
  return truncateText(errorDetail(parsed), 400)
}

/* ───────────────────────────── fallback ───────────────────────────── */

/**
 * Hand a primary failure to the configured fallback provider in the same
 * capability registry. The fallback provider is invoked directly rather than
 * through `ctx.web.*()`, because the seam would re-resolve the *configured*
 * provider — this provider — and recurse.
 *
 * @param {{
 *   operation: 'search' | 'fetch',
 *   endpoint: string,
 *   request: unknown,
 *   signal: AbortSignal | undefined,
 *   providerId: string | undefined,
 *   enabled: boolean,
 *   selfId: string,
 *   primaryError: Error,
 *   lookupFallback: Function,
 *   record: Function,
 *   createError: Function,
 *   invoke: (provider: any) => Promise<any>,
 * }} params - the fallback attempt.
 * @returns {Promise<any>} the fallback provider's result.
 * @throws {Error} the primary error (annotated) when no fallback could or would run.
 */
async function runFallback(params) {
  const { operation, request, signal, providerId, enabled, selfId, primaryError, lookupFallback, record, createError, invoke } = params
  if (!shouldFallback(primaryError)) throw primaryError
  if (enabled !== true) throw primaryError
  if (signal?.aborted === true) throw primaryError
  if (typeof providerId !== 'string' || providerId.trim().length === 0 || providerId === selfId) throw primaryError
  const provider = lookupFallback?.(providerId)
  if (provider === undefined || provider === null) {
    record?.(EVENT_FALLBACK_SKIPPED, { operation, provider: providerId, reason: 'not-registered', code: codeOf(primaryError) })
    throw withNote(
      createError,
      primaryError,
      `The configured fallback ${operation} provider "${providerId}" is not registered, so this failure was returned directly. The web-deepseek-tools bundle expects the previous backend to stay mounted (deepseek-official for search, http for fetch).`,
    )
  }
  let usable = true
  try {
    usable = typeof provider.available !== 'function' || provider.available() === true
  } catch {
    usable = false
  }
  if (!usable) {
    record?.(EVENT_FALLBACK_SKIPPED, { operation, provider: providerId, reason: 'unavailable', code: codeOf(primaryError) })
    throw withNote(
      createError,
      primaryError,
      `The configured fallback ${operation} provider "${providerId}" is registered but unavailable, so this failure was returned directly.`,
    )
  }
  record?.(EVENT_FALLBACK, {
    operation,
    provider: providerId,
    code: codeOf(primaryError),
    httpStatus: primaryError?.httpStatus ?? null,
    message: messageOf(primaryError),
  })
  try {
    return await invoke(provider)
  } catch (fallbackError) {
    if (signal?.aborted === true || fallbackError?.code === 'WEB_ABORTED') throw fallbackError
    throw createError(
      `${messageOf(primaryError)}\n\nThe fallback ${operation} provider "${providerId}" also failed: ${messageOf(fallbackError)}`,
      'WEB_PROVIDER_ERROR',
      { cause: fallbackError },
    )
  }
}

/* ───────────────────────────── helpers ───────────────────────────── */

/** Whether the resolved options are structurally able to dispatch at all (the seam's cheap availability probe). */
function canDispatch(options) {
  return options !== undefined
    && options !== null
    && typeof options.baseURL === 'string'
    && options.baseURL.length > 0
    && Number.isFinite(options.timeoutMs)
    && options.timeoutMs > 0
}

/** Absolute endpoint for the resolved options. */
function endpointForOptions(options, path) {
  if (typeof options.baseURL !== 'string' || options.baseURL.length === 0) {
    throw new Error('web-deepseek-tools: baseURL is empty; the provider cannot dispatch')
  }
  return endpointFor(options.baseURL, path)
}

/**
 * Append the endpoint and the two supported recovery paths to failures that
 * happened after dispatch. Credential failures carry their own guidance and are
 * left alone.
 */
function enrichEndpointError(createError, error, operation, endpoint) {
  // Credential and cancellation failures carry their own guidance; only a
  // post-dispatch transport/HTTP/parse failure benefits from naming the endpoint.
  if (error?.code !== 'WEB_PROVIDER_ERROR' && error?.code !== 'WEB_PROVIDER_TIMEOUT') return error
  const note = `The DeepSeek ${operation} request used endpoint ${JSON.stringify(endpoint)}. That endpoint (baseURL or $DEEPSEEK_TOOLS_BASE_URL) is configured by the web-deepseek-tools bundle; only the user should change it.`
  return withNote(createError, error, note)
}

/** Re-throw a failure with an appended note while preserving its code and HTTP status. */
function withNote(createError, error, note) {
  const wrapped = createError(`${messageOf(error)}\n\n${note}`, codeOf(error), { cause: error })
  if (error?.httpStatus !== undefined) wrapped.httpStatus = error.httpStatus
  return wrapped
}

/** The machine-routable code of any thrown value. */
function codeOf(error) {
  return typeof error?.code === 'string' && error.code.length > 0 ? error.code : 'WEB_PROVIDER_ERROR'
}

/** The human-readable message of any thrown value. */
function messageOf(error) {
  if (error instanceof Error && error.message.length > 0) return error.message
  if (typeof error === 'string' && error.length > 0) return error
  return String(error)
}

/** Build a per-attempt signal: the caller's cancellation plus this attempt's own deadline. */
function attemptSignalFor(signal, timeoutMs) {
  const deadline = AbortSignal.timeout(timeoutMs)
  return signal === undefined ? deadline : AbortSignal.any([signal, deadline])
}

/** Throw the provider's stable cancellation error when the caller already aborted. */
function throwIfAborted(createError, signal) {
  if (signal?.aborted === true) throw abortedError(createError, signal)
}

/** Build the stable cancellation error while retaining the caller's reason. */
function abortedError(createError, signal, fallback) {
  return createError('DeepSeek Search/Fetch aborted', 'WEB_ABORTED', {
    cause: signal?.aborted === true ? signal.reason : fallback,
  })
}

/** Abortable sleep used between retries. */
function delay(ms, signal, createError) {
  if (signal?.aborted === true) return Promise.reject(abortedError(createError, signal))
  return new Promise((resolve, reject) => {
    const cleanup = () => {
      clearTimeout(timer)
      signal?.removeEventListener('abort', onAbort)
    }
    const onAbort = () => {
      cleanup()
      reject(abortedError(createError, signal))
    }
    const timer = setTimeout(() => {
      cleanup()
      resolve()
    }, ms)
    signal?.addEventListener('abort', onAbort, { once: true })
  })
}

/** Exported for the account-route constant's documentation and tests. */
export { ACCOUNT_PROVIDER_ID }
