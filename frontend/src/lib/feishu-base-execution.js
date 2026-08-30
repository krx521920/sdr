const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const SHA256_PATTERN = /^[0-9a-f]{64}$/;
const FEISHU_TARGET_PATTERN =
  /^feishu-base:[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export const FEISHU_BASE_ACTIONS = Object.freeze({
  validateSchema: 'validate_base_schema',
  syncResearch: 'sync_research_result',
  deleteResearch: 'delete_research_record'
});

/** @type {Set<string>} */
const ACTIONS = new Set(Object.values(FEISHU_BASE_ACTIONS));
const SYNC_STATUSES = new Set([
  'pending',
  'pending_approval',
  'queued',
  'syncing',
  'succeeded',
  'failed',
  'unknown',
  'skipped',
  'external_erasure_pending',
  'external_erasure_completed'
]);
const ERASURE_STATUSES = new Set(['none', 'available', 'pending', 'completed', 'unknown']);
const MAPPING_KEYS = new Set([
  'intake_id',
  'company_name',
  'contact_name',
  'email',
  'phone',
  'linkedin_url',
  'website',
  'source',
  'source_record_id',
  'research_summary',
  'research_facts',
  'source_urls',
  'qualification_score',
  'qualification_band',
  'qualification_reasons',
  'assigned_sales',
  'routing_reason',
  'crm_lead_id',
  'processed_at',
  'inspection_status'
]);

const text = (value, maximum) =>
  typeof value === 'string' && value.trim().length <= maximum ? value.trim() : '';
const object = (value) =>
  value && typeof value === 'object' && !Array.isArray(value) ? value : {};

export function validFeishuUuid(value) {
  return typeof value === 'string' && UUID_PATTERN.test(value);
}

/**
 * Build the write-only connection payload. Empty secrets/identifiers are
 * omitted so the backend preserves stored values; enabling a connection never
 * authorizes provider execution.
 * @param {unknown} value
 */
export function buildFeishuBaseConnectionWrite(value) {
  const input = object(value);
  const appId = text(input.appId, 100);
  const appSecret = text(input.appSecret, 4096);
  const appToken = text(input.appToken, 255);
  const tableId = text(input.tableId, 255);
  const rawMapping = object(input.fieldMapping);
  const fieldMapping = {};
  for (const [key, fieldName] of Object.entries(rawMapping)) {
    const normalized = text(fieldName, 100);
    if (!MAPPING_KEYS.has(key) || !normalized) continue;
    fieldMapping[key] = normalized;
  }
  const names = Object.values(fieldMapping);
  if (new Set(names).size !== names.length) return null;
  const body = { is_active: input.isActive === true };
  if (appId) body.app_id = appId;
  if (appSecret) body.app_secret = appSecret;
  if (appToken) body.app_token = appToken;
  if (tableId) body.table_id = tableId;
  if (Object.keys(fieldMapping).length) body.field_mapping = fieldMapping;
  return body;
}

/**
 * Keep only connection state safe to render. Provider identifiers and raw
 * credentials are deliberately discarded even if an older backend returns them.
 * @param {unknown} value
 */
export function normalizeFeishuBaseConnection(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return {
      id: '',
      configured: false,
      secretConfigured: false,
      appIdConfigured: false,
      targetConfigured: false,
      active: false,
      fieldMapping: {},
      lastValidatedAt: '',
      lastSyncAt: '',
      syncSummary: {}
    };
  }
  const connection = object(value);
  const summary = connection.sync_summary;
  const syncSummary = {};
  const fieldMapping = {};
  if (summary && typeof summary === 'object' && !Array.isArray(summary)) {
    for (const key of [
      'total',
      'pending',
      'queued',
      'syncing',
      'succeeded',
      'failed',
      'skipped',
      'unknown',
      'external_erasure_pending',
      'external_erasure_completed'
    ]) {
      const count = Number(summary[key]);
      syncSummary[key] = Number.isSafeInteger(count) && count >= 0 ? count : 0;
    }
  }
  if (
    connection.field_mapping &&
    typeof connection.field_mapping === 'object' &&
    !Array.isArray(connection.field_mapping)
  ) {
    for (const [key, fieldName] of Object.entries(connection.field_mapping)) {
      const normalized = text(fieldName, 100);
      if (MAPPING_KEYS.has(key) && normalized) fieldMapping[key] = normalized;
    }
  }
  return {
    id: validFeishuUuid(connection.id) ? connection.id : '',
    configured:
      connection.configured === true ||
      (validFeishuUuid(connection.id) && connection.app_secret_configured === true),
    secretConfigured: connection.app_secret_configured === true,
    appIdConfigured: connection.app_id_configured === true || Boolean(text(connection.app_id, 100)),
    targetConfigured:
      connection.target_configured === true ||
      (connection.app_token_configured === true && connection.table_id_configured === true) ||
      (Boolean(text(connection.app_token, 255)) && Boolean(text(connection.table_id, 255))),
    active: connection.is_active === true,
    fieldMapping,
    lastValidatedAt: text(connection.last_validated_at, 64),
    lastSyncAt: text(connection.last_sync_at, 64),
    syncSummary
  };
}

/**
 * Normalize an approval intent. Only values needed to create an exact approval
 * may cross the SvelteKit server/page boundary.
 * @param {unknown} value
 * @param {'validate_base_schema' | 'sync_research_result' | 'delete_research_record'} expectedAction
 */
export function normalizeFeishuBaseIntent(value, expectedAction) {
  if (!ACTIONS.has(expectedAction) || !value || typeof value !== 'object' || Array.isArray(value)) {
    return null;
  }
  const response = object(value);
  const intent = object(response.intent);
  if (
    response.approval_required !== true ||
    intent.channel !== 'feishu' ||
    intent.action !== expectedAction
  ) {
    return null;
  }
  const target = text(intent.test_target_identifier, 200);
  const payloadHash = text(intent.payload_hash, 64).toLowerCase();
  const units = Number(intent.units);
  if (
    !FEISHU_TARGET_PATTERN.test(target) ||
    !SHA256_PATTERN.test(payloadHash) ||
    !Number.isSafeInteger(units) ||
    units < 1 ||
    units > 1_000_000
  ) {
    return null;
  }
  return {
    channel: 'feishu',
    action: expectedAction,
    payloadHash,
    testTargetIdentifier: target,
    units
  };
}

/** @param {unknown} approvalId @param {() => string} uuidFactory */
export function buildFeishuBaseApprovedExecution(
  approvalId,
  uuidFactory = () => crypto.randomUUID()
) {
  const approval = text(approvalId, 36);
  const idempotencyKey = text(uuidFactory(), 36);
  if (!validFeishuUuid(approval) || !validFeishuUuid(idempotencyKey)) return null;
  return { approval_id: approval, idempotency_key: idempotencyKey };
}

/**
 * Safe schema validation projection. Field names and provider errors stay server-side.
 * @param {unknown} value
 */
export function normalizeFeishuSchemaResult(value) {
  const result = object(value);
  if (result.valid !== true) return null;
  const fieldCount = Number(result.field_count);
  const mappedFieldCount = Number(result.mapped_field_count);
  return {
    valid: true,
    fieldCount: Number.isSafeInteger(fieldCount) && fieldCount >= 0 ? fieldCount : 0,
    mappedFieldCount:
      Number.isSafeInteger(mappedFieldCount) && mappedFieldCount >= 0 ? mappedFieldCount : 0,
    validatedAt: text(result.validated_at, 64)
  };
}

/**
 * Keep a non-sensitive local ledger projection. Never forward Base record IDs,
 * app/table tokens, request bodies, or provider error details.
 * @param {unknown} payload
 */
export function normalizeFeishuBaseSyncs(payload) {
  const response = object(payload);
  const values = Array.isArray(payload)
    ? payload
    : Array.isArray(response.results)
      ? response.results
      : [];
  const results = [];
  for (const item of values) {
    const row = object(item);
    if (!validFeishuUuid(row.id)) continue;
    const status = SYNC_STATUSES.has(row.status) ? row.status : 'unknown';
    const erasureStatus = ERASURE_STATUSES.has(row.external_erasure_status)
      ? row.external_erasure_status
      : 'none';
    results.push({
      id: row.id,
      intakeId: validFeishuUuid(row.intake_id) ? row.intake_id : '',
      safeLabel: text(row.safe_label, 120) || `Feishu sync ${row.id.slice(0, 8)}`,
      status,
      erasureStatus,
      canDelete: row.can_delete === true && ['available', 'pending'].includes(erasureStatus),
      updatedAt: text(row.updated_at, 64)
    });
  }
  return { count: results.length, results };
}
