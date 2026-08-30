import { error, fail } from '@sveltejs/kit';

import { apiRequest } from '$lib/api-helpers.js';
import {
  buildGovernanceQuery,
  CONTACT_INTENT_CHANNELS,
  CONTACT_INTENT_PURPOSES,
  CONTACT_INTENT_STATUSES,
  EVIDENCE_REVIEW_REASON_CODES,
  isGovernanceUuid,
  normalizeContactIntent,
  normalizeContactIntentList,
  normalizeGovernanceDetail,
  normalizeGovernanceEvidence,
  normalizeGovernanceList,
  parseGovernanceFilters
} from '$lib/matching/governance.js';
import { normalizeMatchingCapabilities } from '$lib/matching/workbench.js';
import { logSafeServerError } from '$lib/server/safe-error-log.js';

const REVIEW_DECISIONS = new Set(['confirm', 'reject']);
const DELETION_ACTIONS = new Set(['request', 'cancel', 'anonymize']);
const REVIEW_REASON_CODES = new Set(EVIDENCE_REVIEW_REASON_CODES);

/** @param {unknown} requestError */
function requestStatus(requestError) {
  const status = Number(/** @type {{ status?: number }} */ (requestError)?.status);
  return Number.isInteger(status) && status >= 100 && status <= 599 ? status : 0;
}

/** @param {unknown} value */
function expectedRevision(value) {
  const number = Number(value);
  return Number.isSafeInteger(number) && number >= 0 ? number : null;
}

/** @param {unknown} value */
function safeDateInput(value) {
  const text = String(value || '').trim();
  if (!text) return '';
  const parsed = new Date(text);
  return Number.isNaN(parsed.getTime()) ? '' : parsed.toISOString();
}

/** @param {unknown} raw */
function safeRunIds(raw) {
  return Array.isArray(raw)
    ? [...new Set(raw.filter(isGovernanceUuid).map(String))].slice(0, 500)
    : [];
}

/** @param {unknown} raw */
function safeRetentionResult(raw) {
  const value =
    raw && typeof raw === 'object' && !Array.isArray(raw)
      ? /** @type {Record<string, unknown>} */ (raw)
      : {};
  const integer = (input) => {
    const number = Number(input);
    return Number.isFinite(number) ? Math.max(0, Math.trunc(number)) : 0;
  };
  return {
    due: integer(value.due),
    restricted: integer(value.restricted),
    anonymized: integer(value.anonymized),
    expired: integer(value.expired),
    recomputed: integer(value.recomputed),
    execute: value.execute === true
  };
}

/** @param {import('@sveltejs/kit').Cookies} cookies @param {App.Locals} locals */
async function matchingCapabilities(cookies, locals) {
  return normalizeMatchingCapabilities(
    await apiRequest('/matching/capabilities/', {}, { cookies, org: locals.org })
  );
}

/**
 * @param {import('@sveltejs/kit').Cookies} cookies
 * @param {App.Locals} locals
 * @param {'manage'|'export'|'delete'|'retention'} permission
 * @param {string} unavailableMessage
 */
async function actionPermission(cookies, locals, permission, unavailableMessage) {
  if (!locals.user?.id || !locals.org?.id) {
    return { error: fail(401, { actionError: 'Organization context is required.' }) };
  }
  try {
    const permissions = await matchingCapabilities(cookies, locals);
    if (permissions[permission] !== true) {
      return { error: fail(403, { actionError: unavailableMessage }) };
    }
    return { permissions };
  } catch (requestError) {
    logSafeServerError('Matching governance action permission check failed', requestError);
    const status = requestStatus(requestError);
    return {
      error: fail(status === 401 || status === 403 ? status : 503, {
        actionError: 'Governance permission could not be verified.'
      })
    };
  }
}

/** @type {import('./$types').PageServerLoad} */
export async function load({ cookies, locals, url }) {
  if (!locals.user?.id || !locals.org?.id) throw error(401, 'Organization context required');
  const filters = parseGovernanceFilters(url.searchParams);
  let permissions;
  try {
    permissions = await matchingCapabilities(cookies, locals);
  } catch (requestError) {
    logSafeServerError('Matching governance capability load failed', requestError);
    const status = requestStatus(requestError);
    throw error(
      status === 401 || status === 403 ? status : 502,
      'Governance access could not be verified'
    );
  }
  if (!permissions.read) {
    throw error(403, 'Matching read access is required to view evidence governance');
  }

  try {
    const list = normalizeGovernanceList(
      await apiRequest(
        `/matching/governance/people/?${buildGovernanceQuery(filters)}`,
        {},
        { cookies, org: locals.org }
      )
    );
    let selected = null;
    let intents = [];
    if (filters.person) {
      const [detailResult, intentResult] = await Promise.all([
        apiRequest(
          `/matching/governance/people/${filters.person}/`,
          {},
          { cookies, org: locals.org }
        ),
        apiRequest(
          `/matching/people/${filters.person}/contact-intents/?limit=100`,
          {},
          { cookies, org: locals.org }
        )
      ]);
      selected = normalizeGovernanceDetail(detailResult);
      if (selected.person.id !== filters.person) {
        throw error(404, 'Governance person not found');
      }
      intents = normalizeContactIntentList(intentResult);
    }
    return {
      permissions,
      filters,
      summary: list.summary,
      people: list.results,
      personCount: list.count,
      selected,
      intents
    };
  } catch (requestError) {
    if (/** @type {{ status?: number }} */ (requestError)?.status === 404) throw requestError;
    logSafeServerError('Matching governance load failed', requestError);
    const status = requestStatus(requestError);
    throw error(
      status === 401 || status === 403 || status === 404 ? status : 502,
      status === 404 ? 'Governance person not found' : 'Evidence governance could not be loaded'
    );
  }
}

/** @type {import('./$types').Actions} */
export const actions = {
  reviewEvidence: async ({ request, cookies, locals }) => {
    const access = await actionPermission(
      cookies,
      locals,
      'manage',
      'You no longer have permission to review evidence.'
    );
    if (access.error) return access.error;
    const form = await request.formData();
    const evidenceId = String(form.get('evidence_id') || '');
    const decision = String(form.get('decision') || '');
    const reasonCode = String(form.get('reason_code') || '');
    const reason = String(form.get('reason') || '')
      .trim()
      .slice(0, 1000);
    const revision = expectedRevision(form.get('expected_revision'));
    const idempotencyKey = String(form.get('idempotency_key') || '');
    if (
      !isGovernanceUuid(evidenceId) ||
      !REVIEW_DECISIONS.has(decision) ||
      !REVIEW_REASON_CODES.has(reasonCode) ||
      revision === null ||
      !isGovernanceUuid(idempotencyKey)
    ) {
      return fail(400, { actionError: 'Choose a valid evidence review decision.' });
    }
    try {
      const result = await apiRequest(
        `/matching/evidence/${evidenceId}/review/`,
        {
          method: 'POST',
          headers: { 'Idempotency-Key': idempotencyKey },
          body: {
            decision,
            reason_code: reasonCode,
            reason,
            expected_revision: revision,
            idempotency_key: idempotencyKey
          }
        },
        { cookies, org: locals.org }
      );
      const evidence = normalizeGovernanceEvidence(result?.evidence ?? result);
      if (evidence.id !== evidenceId) {
        return fail(502, { actionError: 'The reviewed evidence could not be verified safely.' });
      }
      return {
        governanceUpdated: true,
        action: 'reviewEvidence',
        evidence,
        matchRunIds: safeRunIds(result?.match_run_ids)
      };
    } catch (requestError) {
      logSafeServerError('Matching evidence review failed', requestError);
      const status = requestStatus(requestError);
      return fail(status === 401 || status === 403 ? status : status === 409 ? 409 : 400, {
        actionError:
          status === 409
            ? 'This evidence changed. Refresh it before deciding.'
            : status === 403
              ? 'You no longer have permission to review evidence.'
              : 'The evidence review could not be saved.'
      });
    }
  },

  saveIntent: async ({ request, cookies, locals }) => {
    const access = await actionPermission(
      cookies,
      locals,
      'manage',
      'You no longer have permission to record intent.'
    );
    if (access.error) return access.error;
    const form = await request.formData();
    const personId = String(form.get('person_id') || '');
    const state = String(form.get('state') || '');
    const channel = String(form.get('channel') || '');
    const purpose = String(form.get('purpose') || '');
    const observedAt = safeDateInput(form.get('observed_at'));
    const validUntilRaw = String(form.get('valid_until') || '').trim();
    const validUntil = safeDateInput(validUntilRaw);
    const reasonCode = String(form.get('reason_code') || '').trim();
    const revision = expectedRevision(form.get('expected_revision'));
    const idempotencyKey = String(form.get('idempotency_key') || '');
    if (
      !isGovernanceUuid(personId) ||
      !CONTACT_INTENT_STATUSES.includes(state) ||
      !CONTACT_INTENT_CHANNELS.includes(channel) ||
      !CONTACT_INTENT_PURPOSES.includes(purpose) ||
      !/^[a-z0-9_.:-]{1,64}$/.test(reasonCode) ||
      !observedAt ||
      (validUntilRaw && !validUntil) ||
      revision === null ||
      !isGovernanceUuid(idempotencyKey)
    ) {
      return fail(400, { actionError: 'Review the intent, observation date and revision.' });
    }
    try {
      const result = await apiRequest(
        `/matching/people/${personId}/contact-intents/`,
        {
          method: 'POST',
          headers: { 'Idempotency-Key': idempotencyKey },
          body: {
            state,
            channel,
            purpose,
            source: 'manual',
            confidence: 1,
            observed_at: observedAt,
            valid_until: validUntil || null,
            reason_code: reasonCode,
            expected_revision: revision,
            idempotency_key: idempotencyKey
          }
        },
        { cookies, org: locals.org }
      );
      const intent = normalizeContactIntent(result?.intent ?? result);
      if (!intent.id) {
        return fail(502, { actionError: 'The recorded intent could not be verified safely.' });
      }
      return {
        governanceUpdated: true,
        action: 'saveIntent',
        intent,
        matchRunIds: safeRunIds(result?.match_run_ids)
      };
    } catch (requestError) {
      logSafeServerError('Matching contact intent update failed', requestError);
      const statusCode = requestStatus(requestError);
      return fail(
        statusCode === 401 || statusCode === 403 ? statusCode : statusCode === 409 ? 409 : 400,
        {
          actionError:
            statusCode === 409
              ? 'This intent changed. Refresh before recording a new statement.'
              : statusCode === 403
                ? 'You no longer have permission to record intent.'
                : 'The contact intent could not be recorded.'
        }
      );
    }
  },

  deletion: async ({ request, cookies, locals }) => {
    const access = await actionPermission(
      cookies,
      locals,
      'delete',
      'Only an organization administrator can manage deletion.'
    );
    if (access.error) return access.error;
    const form = await request.formData();
    const personId = String(form.get('person_id') || '');
    const deletionAction = String(form.get('deletion_action') || '');
    const confirmation = String(form.get('confirm_person_id') || '');
    const revision = expectedRevision(form.get('expected_revision'));
    const idempotencyKey = String(form.get('idempotency_key') || '');
    if (
      !isGovernanceUuid(personId) ||
      !DELETION_ACTIONS.has(deletionAction) ||
      (deletionAction === 'anonymize' && confirmation !== personId) ||
      revision === null ||
      !isGovernanceUuid(idempotencyKey)
    ) {
      return fail(400, { actionError: 'The deletion request is invalid or not confirmed.' });
    }
    try {
      const result = await apiRequest(
        `/matching/people/${personId}/deletion/`,
        {
          method: 'POST',
          headers: { 'Idempotency-Key': idempotencyKey },
          body: {
            action: deletionAction,
            expected_revision: revision,
            idempotency_key: idempotencyKey,
            ...(deletionAction === 'anonymize' ? { confirm_person_id: confirmation } : {})
          }
        },
        { cookies, org: locals.org }
      );
      return {
        governanceUpdated: true,
        action: 'deletion',
        personId,
        retention: normalizeGovernanceDetail(result).retention
      };
    } catch (requestError) {
      logSafeServerError('Matching person deletion action failed', requestError);
      const status = requestStatus(requestError);
      return fail(status === 401 || status === 403 ? status : status === 409 ? 409 : 400, {
        actionError:
          status === 409
            ? 'This deletion record changed. Refresh before continuing.'
            : status === 403
              ? 'Only an organization administrator can manage deletion.'
              : 'The deletion action could not be completed.'
      });
    }
  },

  retentionScan: async ({ request, cookies, locals }) => {
    const access = await actionPermission(
      cookies,
      locals,
      'retention',
      'Only an organization administrator can run retention scans.'
    );
    if (access.error) return access.error;
    const form = await request.formData();
    const revision = expectedRevision(form.get('expected_revision'));
    const idempotencyKey = String(form.get('idempotency_key') || '');
    if (revision === null || !isGovernanceUuid(idempotencyKey)) {
      return fail(400, { actionError: 'The retention scan request is stale or invalid.' });
    }
    try {
      const result = await apiRequest(
        '/matching/governance/retention/scan/',
        {
          method: 'POST',
          headers: { 'Idempotency-Key': idempotencyKey },
          body: {
            execute: form.has('execute'),
            expected_revision: revision,
            idempotency_key: idempotencyKey
          }
        },
        { cookies, org: locals.org }
      );
      return {
        governanceUpdated: true,
        action: 'retentionScan',
        retentionResult: safeRetentionResult(result)
      };
    } catch (requestError) {
      logSafeServerError('Matching retention scan failed', requestError);
      const status = requestStatus(requestError);
      return fail(status === 401 || status === 403 ? status : status === 409 ? 409 : 400, {
        actionError:
          status === 409
            ? 'Governance data changed. Refresh before running the scan.'
            : status === 403
              ? 'Only an organization administrator can run retention scans.'
              : 'The retention scan could not be completed.'
      });
    }
  }
};
