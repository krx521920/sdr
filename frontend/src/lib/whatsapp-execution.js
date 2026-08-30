const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const SHA256 = /^[0-9a-f]{64}$/;
const MESSAGE_STATUSES = new Set([
  'pending',
  'queued',
  'sending',
  'sent',
  'delivered',
  'read',
  'unknown',
  'failed',
  'skipped'
]);
const EXECUTION_STATUSES = new Set([
  'reserved',
  'sending',
  'accepted',
  'delivered',
  'failed',
  'unknown'
]);
const JOB_STATUSES = new Set(['pending', 'queued', 'running', 'retry_scheduled']);

const object = (value) =>
  value && typeof value === 'object' && !Array.isArray(value) ? value : {};
const text = (value, max) => (typeof value === 'string' ? value.trim().slice(0, max) : '');
const dateText = (value) => {
  const normalized = text(value, 64);
  return normalized && Number.isFinite(Date.parse(normalized)) ? normalized : '';
};

export function validWhatsAppUuid(value) {
  return UUID.test(String(value || ''));
}

/**
 * Project the connection response onto the minimum non-secret shape needed by
 * the outbound settings UI. Token hints, encrypted values and any future
 * provider fields are discarded at the server/page boundary.
 *
 * @param {unknown} raw
 */
export function normalizeWhatsAppConnection(raw) {
  const connection = object(raw);
  if (!Object.keys(connection).length) return null;
  const summary = object(connection.message_summary);
  const count = (value) =>
    Number.isInteger(value) && value >= 0 ? Math.min(value, Number.MAX_SAFE_INTEGER) : 0;

  return {
    phone_number_id: text(connection.phone_number_id, 64),
    business_account_id: text(connection.business_account_id, 64),
    display_phone_number: text(connection.display_phone_number, 64),
    is_active: connection.is_active === true,
    access_token_configured: connection.access_token_configured === true,
    message_summary: {
      sent: count(summary.sent),
      delivered: count(summary.delivered),
      failed: count(summary.failed)
    }
  };
}

/**
 * Whitelist the non-sensitive execution ledger fields that may cross the
 * SvelteKit page boundary. Recipient, template, provider IDs, snapshots and
 * errors are deliberately discarded even if an upstream response adds them.
 *
 * @param {unknown} raw
 * @param {string} expectedCampaignId
 * @param {number} limit
 */
export function normalizeWhatsAppMessageResponse(raw, expectedCampaignId, limit = 100) {
  if (!validWhatsAppUuid(expectedCampaignId)) return { count: 0, results: [] };
  const response = object(raw);
  const rows = Array.isArray(response.results) ? response.results : [];
  const safeLimit = Number.isInteger(limit) ? Math.max(0, Math.min(limit, 100)) : 100;
  const results = [];

  for (const rawRow of rows) {
    if (results.length >= safeLimit) break;
    const row = object(rawRow);
    const id = text(row.id, 36);
    const campaignId = text(row.campaign_id, 36);
    const prospectId = text(row.prospect_id, 36);
    const status = text(row.status, 24).toLowerCase();
    const executionRequestId = row.execution_request_id ? text(row.execution_request_id, 36) : null;
    const executionStatus = row.execution_status
      ? text(row.execution_status, 24).toLowerCase()
      : null;

    if (
      !validWhatsAppUuid(id) ||
      campaignId !== expectedCampaignId ||
      !validWhatsAppUuid(prospectId) ||
      !MESSAGE_STATUSES.has(status) ||
      (executionRequestId !== null && !validWhatsAppUuid(executionRequestId)) ||
      (executionStatus !== null && !EXECUTION_STATUSES.has(executionStatus)) ||
      (executionRequestId === null) !== (executionStatus === null)
    ) {
      continue;
    }

    results.push({
      id,
      campaignId,
      prospectId,
      status,
      executionRequestId,
      executionStatus,
      createdAt: dateText(row.created_at)
    });
  }

  return { count: results.length, results };
}

/**
 * Validate an approval-required response and retain only the exact non-PII
 * values needed to issue a Channel Safety approval.
 *
 * @param {unknown} raw
 * @param {string} expectedMessageId
 */
export function normalizeWhatsAppApprovalRequired(raw, expectedMessageId) {
  if (!validWhatsAppUuid(expectedMessageId)) return null;
  const response = object(raw);
  const intent = object(response.intent);
  const messageId = text(intent.message_id, 36);
  const targetHash = text(intent.target_sha256, 64).toLowerCase();
  const payloadHash = text(intent.payload_sha256, 64).toLowerCase();
  const units = Number(intent.units);

  if (
    response.approval_required !== true ||
    intent.channel !== 'whatsapp' ||
    intent.action !== 'send_message' ||
    messageId !== expectedMessageId ||
    !SHA256.test(targetHash) ||
    !SHA256.test(payloadHash) ||
    units !== 1
  ) {
    return null;
  }

  return {
    action: 'send_message',
    messageId,
    targetHash,
    payloadHash,
    units: 1
  };
}

/** @param {unknown} approvalId */
export function buildWhatsAppApprovedExecution(approvalId) {
  const approval = text(approvalId, 36);
  return validWhatsAppUuid(approval) ? { approval_id: approval } : null;
}

/**
 * Accept only a safe acknowledgement of the durable execution state.
 * `jobId` is optional because an idempotent ACCEPTED/DELIVERED replay performs
 * no enqueue and therefore has no new job.
 *
 * @param {unknown} raw
 */
export function normalizeWhatsAppExecutionResult(raw) {
  const response = object(raw);
  const requestId = text(response.execution_request_id, 36);
  const executionStatus = text(response.execution_status, 24).toLowerCase();
  const jobStatus = response.status ? text(response.status, 24).toLowerCase() : null;
  const jobId = response.job_id ? text(response.job_id, 36) : null;
  const replayed = response.replayed === true;
  const freshReservation = executionStatus === 'reserved';
  const completedReplay = replayed && ['accepted', 'delivered'].includes(executionStatus);
  if (
    !validWhatsAppUuid(requestId) ||
    !EXECUTION_STATUSES.has(executionStatus) ||
    (!freshReservation && !completedReplay) ||
    (freshReservation && (!validWhatsAppUuid(jobId) || !JOB_STATUSES.has(jobStatus))) ||
    (completedReplay && (jobId !== null || jobStatus !== null))
  ) {
    return null;
  }
  return {
    executionRequestId: requestId,
    executionStatus,
    jobStatus,
    replayed,
    jobId
  };
}
