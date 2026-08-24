/**
 * Server-only error logging helpers.
 *
 * Axios errors retain the full request configuration, request object and
 * response body. Logging those objects can expose bearer tokens, refresh
 * tokens and OAuth PKCE material, so callers only receive an allow-listed
 * transport summary.
 */

const TRACE_HEADER_NAMES = ['x-trace-id', 'x-request-id', 'traceparent'];
const SAFE_ERROR_CODES = new Set([
  'EAI_AGAIN',
  'ECONNABORTED',
  'ECONNREFUSED',
  'ECONNRESET',
  'EHOSTUNREACH',
  'ENETUNREACH',
  'ENOTFOUND',
  'ETIMEDOUT',
  'ERR_BAD_OPTION',
  'ERR_BAD_OPTION_VALUE',
  'ERR_BAD_REQUEST',
  'ERR_BAD_RESPONSE',
  'ERR_CANCELED',
  'ERR_DEPRECATED',
  'ERR_FR_TOO_MANY_REDIRECTS',
  'ERR_INVALID_URL',
  'ERR_NETWORK',
  'ERR_NOT_SUPPORT'
]);
const HEX_TRACE_ID_RE = /^[a-f0-9]{16,64}$/i;
const UUID_TRACE_ID_RE = /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i;
const TRACEPARENT_RE = /^[a-f0-9]{2}-[a-f0-9]{32}-[a-f0-9]{16}-[a-f0-9]{2}$/i;

/**
 * @typedef {{ status?: number, code?: string, traceId?: string }} SafeErrorMetadata
 * @typedef {{ error: (message: string, metadata: SafeErrorMetadata) => void }} ErrorLogger
 */

/** @param {unknown} value @returns {string|undefined} */
function safeErrorCode(value) {
  if (typeof value !== 'string') return undefined;
  const normalized = value.trim();
  return SAFE_ERROR_CODES.has(normalized) ? normalized : undefined;
}

/** @param {unknown} value @returns {string|undefined} */
function safeTraceId(value) {
  if (typeof value !== 'string') return undefined;
  const normalized = value.trim();
  if (
    !HEX_TRACE_ID_RE.test(normalized) &&
    !UUID_TRACE_ID_RE.test(normalized) &&
    !TRACEPARENT_RE.test(normalized)
  ) {
    return undefined;
  }
  return normalized;
}

/**
 * Read one explicitly allowed header from either AxiosHeaders or a plain
 * headers object. No other header is copied or serialized.
 *
 * @param {unknown} headers
 * @param {string} name
 * @returns {unknown}
 */
function readHeader(headers, name) {
  try {
    if (!headers || typeof headers !== 'object') return undefined;
    const headerContainer = /** @type {{ get?: (name: string) => unknown }} */ (headers);
    if (typeof headerContainer.get === 'function') {
      const value = headerContainer.get(name);
      if (value !== undefined && value !== null) return value;
    }
    const headerRecord = /** @type {Record<string, unknown>} */ (headers);
    const matchingKey = Object.keys(headerRecord).find((key) => key.toLowerCase() === name);
    return matchingKey ? headerRecord[matchingKey] : undefined;
  } catch {
    return undefined;
  }
}

/** @param {Record<string, any>} error @returns {string|undefined} */
function getTraceId(error) {
  try {
    const headers = error.response?.headers;
    for (const name of TRACE_HEADER_NAMES) {
      const traceId = safeTraceId(readHeader(headers, name));
      if (traceId) return traceId;
    }
    return undefined;
  } catch {
    return undefined;
  }
}

/**
 * Convert an unknown error into allow-listed, non-sensitive metadata.
 * Deliberately excludes message, stack, config, headers, request and response
 * data because each may contain authentication material or customer data.
 *
 * @param {unknown} error
 * @returns {SafeErrorMetadata}
 */
export function getSafeErrorMetadata(error) {
  try {
    if (!error || typeof error !== 'object') return { code: 'unknown_error' };
    const errorRecord = /** @type {Record<string, any>} */ (error);
    /** @type {SafeErrorMetadata} */
    const metadata = {};
    const status = errorRecord.response?.status ?? errorRecord.status;
    const code = safeErrorCode(errorRecord.code);
    const traceId = getTraceId(errorRecord);
    if (Number.isInteger(status) && status >= 100 && status <= 599) metadata.status = status;
    if (code) metadata.code = code;
    if (traceId) metadata.traceId = traceId;
    if (Object.keys(metadata).length === 0) metadata.code = 'unknown_error';
    return metadata;
  } catch {
    return { code: 'unknown_error' };
  }
}

/**
 * Log a server-side request failure without serializing the original error.
 * Event names must be static and must not contain user or provider input.
 * Logging failures must not break authentication or organization switching.
 *
 * @param {string} message
 * @param {unknown} error
 * @param {ErrorLogger} [logger]
 */
export function logSafeServerError(message, error, logger = console) {
  const metadata = getSafeErrorMetadata(error);
  try {
    logger.error(message, metadata);
  } catch {
    // Authentication behavior must not depend on the logging transport.
  }
}
