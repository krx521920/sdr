const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const SHA256 = /^[0-9a-f]{64}$/;
const INTENT_ACTIONS = new Set(['search_people', 'enrich_person']);
const CANDIDATE_STATUSES = new Set([
  'pending_enrichment_approval',
  'enrichment_reserved',
  'import_queued',
  'imported',
  'import_review_required',
  'import_failed',
  'import_retry_required',
  'unknown',
  'skipped'
]);

const object = (value) =>
  value && typeof value === 'object' && !Array.isArray(value) ? value : {};
const text = (value, max) => (typeof value === 'string' ? value.trim().slice(0, max) : '');

export function validApolloUuid(value) {
  return UUID.test(String(value || ''));
}

/**
 * Only expose the exact non-PII fields an administrator needs to issue an
 * approval. Provider IDs, provider payloads and the target hash are discarded.
 *
 * @param {unknown} raw
 * @param {'search_people' | 'enrich_person'} expectedAction
 * @param {string} expectedTargetIdentifier
 */
export function normalizeApolloIntent(raw, expectedAction, expectedTargetIdentifier) {
  const value = object(raw);
  const action = text(value.action, 64);
  const payloadHash = text(value.payload_hash, 64).toLowerCase();
  const targetIdentifier = text(value.test_target_identifier, 160);
  const units = Number(value.units);

  if (
    value.channel !== 'apollo' ||
    !INTENT_ACTIONS.has(action) ||
    action !== expectedAction ||
    !SHA256.test(payloadHash) ||
    targetIdentifier !== expectedTargetIdentifier ||
    !Number.isInteger(units) ||
    units < 1 ||
    units > 1_000_000
  ) {
    return null;
  }

  return {
    action,
    payloadHash,
    testTargetIdentifier: targetIdentifier,
    units
  };
}

/**
 * Validate an approval-required response and return only its safe execution
 * intent. Merely receiving this response never means a job was queued.
 *
 * @param {unknown} raw
 * @param {'search_people' | 'enrich_person'} expectedAction
 * @param {string} expectedTargetIdentifier
 */
export function normalizeApolloApprovalRequired(raw, expectedAction, expectedTargetIdentifier) {
  const value = object(raw);
  if (value.status !== 'approval_required') return null;
  return normalizeApolloIntent(value.intent, expectedAction, expectedTargetIdentifier);
}

/**
 * Generate a fresh request idempotency key on the trusted SvelteKit server.
 * No browser-supplied idempotency key is accepted.
 *
 * @param {unknown} approvalId
 * @param {() => string} uuidFactory
 */
export function buildApolloApprovedExecution(approvalId, uuidFactory = () => crypto.randomUUID()) {
  const normalizedApprovalId = text(approvalId, 36);
  if (!validApolloUuid(normalizedApprovalId)) return null;
  const idempotencyKey = uuidFactory();
  if (!validApolloUuid(idempotencyKey)) {
    throw new Error('The server could not generate a valid idempotency key.');
  }
  return {
    approval_id: normalizedApprovalId,
    idempotency_key: idempotencyKey
  };
}

/**
 * Whitelist candidate fields before SvelteKit serializes them to the browser.
 * The returned objects intentionally contain no provider ID, request ID,
 * person name, email, URL or raw payload.
 *
 * @param {unknown} raw
 * @param {string} expectedSourceId
 * @param {number} limit
 */
export function normalizeApolloCandidateResponse(raw, expectedSourceId, limit = 100) {
  if (!validApolloUuid(expectedSourceId)) return { count: 0, results: [] };
  const value = object(raw);
  const rows = Array.isArray(value.results) ? value.results : [];
  const safeLimit = Number.isInteger(limit) ? Math.max(0, Math.min(limit, 100)) : 100;
  const results = [];

  for (const rawRow of rows) {
    if (results.length >= safeLimit) break;
    const row = object(rawRow);
    const id = text(row.id, 36);
    const sourceId = text(row.source_id, 36);
    const safeLabel = text(row.safe_label, 120);
    const status = text(row.status, 40);
    if (
      !validApolloUuid(id) ||
      sourceId !== expectedSourceId ||
      !safeLabel ||
      !CANDIDATE_STATUSES.has(status)
    ) {
      continue;
    }
    results.push({ id, safeLabel, status });
  }

  return { count: results.length, results };
}
