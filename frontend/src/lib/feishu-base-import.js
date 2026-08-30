const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const SHA256_PATTERN = /^[0-9a-f]{64}$/i;
const TARGET_PATTERN =
  /^feishu-base:[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export const FEISHU_PERSON_IMPORT_ACTION = 'import_person_records';
export const FEISHU_PERSON_IMPORT_DEFAULT_LIMIT = 100;
export const FEISHU_PERSON_IMPORT_MAX_LIMIT = 500;

export const FEISHU_PERSON_IMPORT_FIELDS = Object.freeze([
  { target: 'display_name', label: 'Display name', group: 'Person' },
  { target: 'first_name', label: 'First name', group: 'Person' },
  { target: 'last_name', label: 'Last name', group: 'Person' },
  { target: 'current_title', label: 'Current title', group: 'Person' },
  { target: 'current_company', label: 'Current company', group: 'Person' },
  { target: 'location', label: 'Location', group: 'Person' },
  { target: 'email', label: 'Email', group: 'Identity', identity: true },
  { target: 'phone', label: 'Phone', group: 'Identity', identity: true },
  { target: 'linkedin', label: 'LinkedIn', group: 'Identity', identity: true },
  { target: 'evidence_summary', label: 'Evidence summary', group: 'Evidence' },
  { target: 'observed_at', label: 'Observed at', group: 'Evidence' }
]);

const TARGETS = new Set(FEISHU_PERSON_IMPORT_FIELDS.map((field) => field.target));
const IDENTITY_TARGETS = new Set(['email', 'phone', 'linkedin']);
const NAME_TARGETS = new Set(['display_name', 'first_name', 'last_name']);
const IMPORT_STATUSES = new Set(['queued', 'reading', 'previewed', 'failed', 'unknown']);

/** @param {unknown} value @returns {Record<string, any>} */
function object(value) {
  return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
}

/** @param {unknown} value @param {number} maximum */
function text(value, maximum) {
  return typeof value === 'string' && value.trim().length <= maximum ? value.trim() : '';
}

/** @param {unknown} value */
function safeCount(value) {
  const count = Number(value);
  return Number.isSafeInteger(count) && count >= 0 && count <= FEISHU_PERSON_IMPORT_MAX_LIMIT
    ? count
    : 0;
}

/** @param {unknown} value */
export function validFeishuImportUuid(value) {
  return typeof value === 'string' && UUID_PATTERN.test(value);
}

/**
 * Validate a one-time mapping without reflecting provider field names in errors.
 * Remote record identifiers are intentionally not valid targets.
 * @param {unknown} value
 */
export function validateFeishuPersonImportMapping(value) {
  const input = object(value);
  const mapping = {};
  for (const [target, providerField] of Object.entries(input)) {
    if (!TARGETS.has(target)) {
      return { mapping: null, error: 'The field mapping contains an unsupported target.' };
    }
    const normalized = text(providerField, 100);
    if (providerField && !normalized) {
      return { mapping: null, error: 'Each provider field name must be 100 characters or fewer.' };
    }
    if (normalized) mapping[target] = normalized;
  }
  if (new Set(Object.values(mapping)).size !== Object.values(mapping).length) {
    return { mapping: null, error: 'Each provider field may be mapped only once.' };
  }
  if (![...NAME_TARGETS].some((target) => mapping[target])) {
    return { mapping: null, error: 'Map a display name, first name, or last name.' };
  }
  if (![...IDENTITY_TARGETS].some((target) => mapping[target])) {
    return { mapping: null, error: 'Map at least one identity: email, phone, or LinkedIn.' };
  }
  return { mapping, error: '' };
}

/** @param {unknown} mapping @param {unknown} limit */
export function buildFeishuPersonImportPreview(mapping, limit) {
  const validated = validateFeishuPersonImportMapping(mapping);
  const normalizedLimit = Number(limit ?? FEISHU_PERSON_IMPORT_DEFAULT_LIMIT);
  if (
    !validated.mapping ||
    !Number.isSafeInteger(normalizedLimit) ||
    normalizedLimit < 1 ||
    normalizedLimit > FEISHU_PERSON_IMPORT_MAX_LIMIT
  ) {
    return null;
  }
  return { mapping: validated.mapping, limit: normalizedLimit };
}

/**
 * Keep only the opaque values required to issue an exact Channel Safety approval.
 * Provider schema, credentials, records, and response bodies are discarded.
 * @param {unknown} value
 */
export function normalizeFeishuPersonImportIntent(value) {
  const response = object(value);
  const intent = object(response.intent);
  const targetHash = text(intent.target_hash, 64).toLowerCase();
  const payloadHash = text(intent.payload_hash, 64).toLowerCase();
  const testTargetIdentifier = text(intent.test_target_identifier, 100);
  const units = Number(intent.units);
  if (
    response.approval_required !== true ||
    intent.channel !== 'feishu' ||
    intent.action !== FEISHU_PERSON_IMPORT_ACTION ||
    !SHA256_PATTERN.test(targetHash) ||
    !SHA256_PATTERN.test(payloadHash) ||
    !TARGET_PATTERN.test(testTargetIdentifier) ||
    units !== 1
  ) {
    return null;
  }
  return {
    channel: 'feishu',
    action: FEISHU_PERSON_IMPORT_ACTION,
    targetHash,
    payloadHash,
    testTargetIdentifier,
    units: 1
  };
}

/**
 * Build the second-stage body. The server supplies the idempotency UUID so it
 * never has to be stored by the browser.
 * @param {unknown} mapping
 * @param {unknown} limit
 * @param {unknown} approvalId
 * @param {() => string} uuidFactory Returns one stable approval-scoped UUID.
 */
export function buildFeishuPersonImportExecution(
  mapping,
  limit,
  approvalId,
  uuidFactory = () => crypto.randomUUID()
) {
  const preview = buildFeishuPersonImportPreview(mapping, limit);
  const approval = text(approvalId, 36);
  const idempotencyKey = text(uuidFactory(), 36);
  if (!preview || !validFeishuImportUuid(approval) || !validFeishuImportUuid(idempotencyKey)) {
    return null;
  }
  return {
    ...preview,
    approval_id: approval,
    idempotency_key: idempotencyKey
  };
}

/**
 * Normalize the async ledger to a fixed safe projection. URLs, hashes, mapping,
 * provider errors, cell values, and remote identifiers never cross this boundary.
 * @param {unknown} value
 */
export function normalizeFeishuPersonImportExecution(value) {
  const input = object(value);
  const id = text(input.id, 36);
  const jobId = text(input.job_id, 36);
  const batchId = text(input.batch_id, 36);
  const status = text(input.status, 32).toLowerCase();
  if (!validFeishuImportUuid(id) || !IMPORT_STATUSES.has(status)) return null;
  if (jobId && !validFeishuImportUuid(jobId)) return null;
  if (batchId && !validFeishuImportUuid(batchId)) return null;
  const nestedCounts = object(input.counts);
  const errorCode = text(input.error_code, 100);
  return {
    id,
    status,
    jobId,
    batchId,
    replayed: input.replayed === true,
    totalCount: safeCount(input.total_count ?? nestedCounts.total),
    readyCount: safeCount(input.ready_count ?? nestedCounts.ready),
    invalidCount: safeCount(input.invalid_count ?? nestedCounts.invalid),
    errorCode: /^[a-z0-9_]+$/.test(errorCode) ? errorCode : '',
    createdAt: text(input.created_at, 64),
    completedAt: text(input.completed_at, 64)
  };
}

/** @param {unknown} value */
export function normalizeFeishuPersonImportConnection(value) {
  const connection = object(value);
  return {
    configured:
      validFeishuImportUuid(connection.id) &&
      connection.app_id_configured === true &&
      connection.app_secret_configured === true &&
      connection.app_token_configured === true &&
      connection.table_id_configured === true,
    active: connection.is_active === true
  };
}
