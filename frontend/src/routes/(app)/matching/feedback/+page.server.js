import { error, fail } from '@sveltejs/kit';

import { apiRequest } from '$lib/api-helpers.js';
import {
  ACCURACY_LABELS,
  buildFeedbackQuery,
  normalizeCalibrationSuggestion,
  normalizeFeedbackDetail,
  normalizeFeedbackOverview,
  normalizeFeedbackQueueItem,
  normalizeSuggestionList,
  normalizeWeightVersion,
  OUTCOME_CODES,
  parseEvidenceAssessments,
  parseFeedbackFilters,
  REJECTION_REASON_CODES,
  safeCode,
  safeDate,
  safeInteger,
  SUGGESTION_REVIEW_ACTIONS
} from '$lib/matching/feedback.js';
import {
  isUuid,
  normalizeMatchingCapabilities,
  OPPORTUNITY_TYPES
} from '$lib/matching/workbench.js';
import { logSafeServerError } from '$lib/server/safe-error-log.js';

/** @param {unknown} requestError */
function requestStatus(requestError) {
  const status = Number(/** @type {{ status?: number }} */ (requestError)?.status);
  if (Number.isInteger(status) && status >= 100 && status <= 599) return status;
  const message = requestError instanceof Error ? requestError.message : '';
  const match = /^HTTP (\d{3})\b/.exec(message);
  return match ? Number(match[1]) : 0;
}

/** @param {unknown} value */
function expectedRevision(value) {
  const number = Number(value);
  return Number.isSafeInteger(number) && number >= 0 ? number : null;
}

/** @param {import('@sveltejs/kit').Cookies} cookies @param {App.Locals} locals */
async function capabilities(cookies, locals) {
  return normalizeMatchingCapabilities(
    await apiRequest('/matching/capabilities/', {}, { cookies, org: locals.org })
  );
}

/**
 * @param {import('@sveltejs/kit').Cookies} cookies
 * @param {App.Locals} locals
 * @param {'feedback'|'calibrate'} capability
 */
async function requireCapability(cookies, locals, capability) {
  if (!locals.user?.id || !locals.org?.id) {
    return { failure: fail(401, { actionError: 'Organization context is required.' }) };
  }
  try {
    const permissions = await capabilities(cookies, locals);
    if (permissions[capability] !== true) {
      return {
        failure: fail(403, {
          actionError:
            capability === 'calibrate'
              ? 'Only an authorized administrator can calibrate matching weights.'
              : 'You no longer have permission to record matching feedback.'
        })
      };
    }
    return { permissions };
  } catch (requestError) {
    logSafeServerError('Matching feedback permission check failed', requestError);
    const status = requestStatus(requestError);
    return {
      failure: fail(status === 401 || status === 403 ? status : 503, {
        actionError: 'Matching feedback permission could not be verified.'
      })
    };
  }
}

/** @param {unknown} raw */
function results(raw) {
  if (Array.isArray(raw)) return raw;
  const value = raw && typeof raw === 'object' ? /** @type {Record<string, unknown>} */ (raw) : {};
  return Array.isArray(value.results) ? value.results : [];
}

/** @param {unknown} raw */
function record(raw) {
  return raw && typeof raw === 'object' && !Array.isArray(raw)
    ? /** @type {Record<string, unknown>} */ (raw)
    : {};
}

/** @type {import('./$types').PageServerLoad} */
export async function load({ cookies, locals, url }) {
  if (!locals.user?.id || !locals.org?.id) throw error(401, 'Organization context required');
  const filters = parseFeedbackFilters(url.searchParams);
  let permissions;
  try {
    permissions = await capabilities(cookies, locals);
  } catch (requestError) {
    logSafeServerError('Matching feedback capability load failed', requestError);
    const status = requestStatus(requestError);
    throw error(
      status === 401 || status === 403 ? status : 502,
      'Feedback access could not be verified'
    );
  }
  if (!permissions.read) throw error(403, 'Matching read access is required to view feedback');

  const query = buildFeedbackQuery(filters);
  try {
    const [overviewRaw, insightsRaw, queueRaw, policiesRaw, suggestionsRaw] = await Promise.all([
      apiRequest(`/matching/feedback/overview/?${query}`, {}, { cookies, org: locals.org }),
      apiRequest(`/matching/feedback/insights/?${query}`, {}, { cookies, org: locals.org }),
      apiRequest(`/matching/feedback/matches/?${query}`, {}, { cookies, org: locals.org }),
      apiRequest(`/matching/scoring-policies/?${query}`, {}, { cookies, org: locals.org }),
      apiRequest(`/matching/weight-suggestions/?${query}`, {}, { cookies, org: locals.org })
    ]);
    const queue = results(queueRaw)
      .map(normalizeFeedbackQueueItem)
      .filter((item) => item.matchId)
      .slice(0, 100);
    const policies = results(policiesRaw).map(record);
    const versionResponses = await Promise.all(
      policies
        .filter((policy) => isUuid(policy.id))
        .map(async (policy) => ({
          policy,
          versions: await apiRequest(
            `/matching/scoring-policies/${String(policy.id)}/versions/`,
            {},
            { cookies, org: locals.org }
          )
        }))
    );
    const weightVersions = versionResponses
      .flatMap(({ policy, versions }) =>
        results(versions).map((version) =>
          normalizeWeightVersion({
            ...record(version),
            opportunity_type: policy.opportunity_type,
            policy_revision: policy.revision
          })
        )
      )
      .filter((version) => version.id)
      .slice(0, 200);
    const suggestions = normalizeSuggestionList({
      results: results(suggestionsRaw).map((item) => {
        const suggestion = record(item);
        const policy = policies.find((entry) => entry.id === suggestion.policy) || {};
        const activeVersion = record(policy.active_version_detail);
        return {
          ...suggestion,
          current_weights: activeVersion.dimension_weights,
          current_weight_version: activeVersion.version
        };
      })
    });
    let selected = null;
    if (filters.match) {
      selected = normalizeFeedbackDetail(
        await apiRequest(
          `/matching/feedback/matches/${filters.match}/`,
          {},
          { cookies, org: locals.org }
        )
      );
      if (selected.match.id !== filters.match) throw error(404, 'Feedback match not found');
    }
    return {
      permissions,
      filters,
      overview: normalizeFeedbackOverview({
        ...record(overviewRaw),
        insights: record(insightsRaw)
      }),
      queue,
      queueCount: safeInteger(record(queueRaw).count),
      weightVersions,
      suggestions,
      selectedSuggestion: suggestions.find((item) => item.id === filters.suggestion) || null,
      selected
    };
  } catch (requestError) {
    if (requestStatus(requestError) === 404) throw requestError;
    logSafeServerError('Matching feedback load failed', requestError);
    const status = requestStatus(requestError);
    throw error(
      status === 401 || status === 403 || status === 404 ? status : 502,
      status === 404 ? 'Feedback item not found' : 'Matching feedback could not be loaded'
    );
  }
}

/** @type {import('./$types').Actions} */
export const actions = {
  saveFeedback: async ({ request, cookies, locals }) => {
    const access = await requireCapability(cookies, locals, 'feedback');
    if (access.failure) return access.failure;
    const form = await request.formData();
    const matchId = String(form.get('match_id') || '');
    const matchStatus = String(form.get('match_status') || '');
    const verdict = String(form.get('accuracy') || '');
    const rejectionReason = String(form.get('rejection_reason_code') || '');
    const revision = expectedRevision(form.get('expected_revision'));
    const rankingRevision = expectedRevision(form.get('expected_ranking_revision'));
    const idempotencyKey = String(form.get('idempotency_key') || '');
    const attributions = parseEvidenceAssessments(String(form.get('evidence_assessments') || ''));
    const reasonCode = matchStatus === 'rejected' ? rejectionReason : 'assessment_recorded';
    if (
      !isUuid(matchId) ||
      !ACCURACY_LABELS.includes(verdict) ||
      (matchStatus === 'rejected' && !REJECTION_REASON_CODES.includes(rejectionReason)) ||
      !safeCode(reasonCode) ||
      revision === null ||
      rankingRevision === null ||
      !isUuid(idempotencyKey)
    ) {
      return fail(400, { actionError: 'Review the accuracy, rejection reason and revision.' });
    }
    try {
      const result = await apiRequest(
        `/matching/matches/${matchId}/feedback/`,
        {
          method: 'POST',
          headers: { 'Idempotency-Key': idempotencyKey },
          body: {
            expected_revision: revision,
            expected_ranking_revision: rankingRevision,
            verdict,
            reason_code: reasonCode,
            note: '',
            occurred_at: new Date().toISOString(),
            attributions
          }
        },
        { cookies, org: locals.org }
      );
      if (!result || typeof result !== 'object') {
        return fail(502, { actionError: 'The saved feedback could not be verified safely.' });
      }
      return { feedbackUpdated: true, action: 'feedback' };
    } catch (requestError) {
      logSafeServerError('Matching feedback save failed', requestError);
      const status = requestStatus(requestError);
      return fail(status === 401 || status === 403 ? status : status === 409 ? 409 : 400, {
        actionError:
          status === 409
            ? 'This match changed. The latest version was loaded; review your unchanged draft.'
            : status === 403
              ? 'You no longer have permission to record matching feedback.'
              : 'Matching feedback could not be saved.'
      });
    }
  },

  recordOutcome: async ({ request, cookies, locals }) => {
    const access = await requireCapability(cookies, locals, 'feedback');
    if (access.failure) return access.failure;
    const form = await request.formData();
    const matchId = String(form.get('match_id') || '');
    const outcomeCode = String(form.get('outcome_code') || '');
    const occurredAt = safeDate(form.get('occurred_at'));
    const revision = expectedRevision(form.get('expected_revision'));
    const rankingRevision = expectedRevision(form.get('expected_ranking_revision'));
    const idempotencyKey = String(form.get('idempotency_key') || '');
    if (
      !isUuid(matchId) ||
      !OUTCOME_CODES.includes(outcomeCode) ||
      !occurredAt ||
      revision === null ||
      rankingRevision === null ||
      !isUuid(idempotencyKey)
    ) {
      return fail(400, { actionError: 'Choose a valid outcome, date and revision.' });
    }
    try {
      const result = await apiRequest(
        `/matching/matches/${matchId}/outcomes/`,
        {
          method: 'POST',
          headers: { 'Idempotency-Key': idempotencyKey },
          body: {
            expected_revision: revision,
            expected_ranking_revision: rankingRevision,
            outcome_code: outcomeCode,
            reason_code: 'observed_outcome',
            note: '',
            occurred_at: occurredAt,
            attributions: []
          }
        },
        { cookies, org: locals.org }
      );
      if (!result || typeof result !== 'object') {
        return fail(502, { actionError: 'The recorded outcome could not be verified safely.' });
      }
      return { feedbackUpdated: true, action: 'outcome' };
    } catch (requestError) {
      logSafeServerError('Matching outcome save failed', requestError);
      const status = requestStatus(requestError);
      return fail(status === 401 || status === 403 ? status : status === 409 ? 409 : 400, {
        actionError:
          status === 409
            ? 'Feedback changed. The latest version was loaded; review your unchanged outcome.'
            : status === 403
              ? 'You no longer have permission to record matching outcomes.'
              : 'The matching outcome could not be recorded.'
      });
    }
  },

  generateSuggestions: async ({ request, cookies, locals }) => {
    const access = await requireCapability(cookies, locals, 'calibrate');
    if (access.failure) return access.failure;
    const form = await request.formData();
    const opportunityType = String(form.get('opportunity_type') || '');
    const revision = expectedRevision(form.get('expected_revision'));
    const idempotencyKey = String(form.get('idempotency_key') || '');
    if (
      !OPPORTUNITY_TYPES.includes(opportunityType) ||
      revision === null ||
      !isUuid(idempotencyKey)
    ) {
      return fail(400, { actionError: 'The suggestion request is stale or invalid.' });
    }
    try {
      const result = await apiRequest(
        '/matching/weight-suggestions/generate/',
        {
          method: 'POST',
          headers: { 'Idempotency-Key': idempotencyKey },
          body: { opportunity_type: opportunityType, expected_revision: revision }
        },
        { cookies, org: locals.org }
      );
      const suggestion = normalizeCalibrationSuggestion(result?.suggestion ?? result);
      if (!suggestion.id) {
        return fail(502, { actionError: 'The generated suggestion could not be verified safely.' });
      }
      return { feedbackUpdated: true, action: 'generateSuggestion', suggestion };
    } catch (requestError) {
      logSafeServerError('Matching suggestion generation failed', requestError);
      const status = requestStatus(requestError);
      return fail(status === 401 || status === 403 ? status : status === 409 ? 409 : 400, {
        actionError:
          status === 409
            ? 'The scoring policy changed. Refresh before generating another suggestion.'
            : 'A safe weight suggestion could not be generated.'
      });
    }
  },

  reviewSuggestion: async ({ request, cookies, locals }) => {
    const access = await requireCapability(cookies, locals, 'calibrate');
    if (access.failure) return access.failure;
    const form = await request.formData();
    const suggestionId = String(form.get('suggestion_id') || '');
    const action = String(form.get('review_action') || '');
    const reasonCode = String(form.get('reason_code') || '');
    const revision = expectedRevision(form.get('expected_revision'));
    const idempotencyKey = String(form.get('idempotency_key') || '');
    if (
      !isUuid(suggestionId) ||
      !SUGGESTION_REVIEW_ACTIONS.includes(action) ||
      !safeCode(reasonCode) ||
      revision === null ||
      !isUuid(idempotencyKey)
    ) {
      return fail(400, { actionError: 'The suggestion review is stale or invalid.' });
    }
    try {
      const result = await apiRequest(
        `/matching/weight-suggestions/${suggestionId}/review/`,
        {
          method: 'POST',
          headers: { 'Idempotency-Key': idempotencyKey },
          body: { action, reason_code: reasonCode, expected_revision: revision }
        },
        { cookies, org: locals.org }
      );
      const suggestion = normalizeCalibrationSuggestion(result?.suggestion ?? result);
      if (!suggestion.id) {
        return fail(502, { actionError: 'The reviewed suggestion could not be verified safely.' });
      }
      return { feedbackUpdated: true, action: 'reviewSuggestion', suggestion };
    } catch (requestError) {
      logSafeServerError('Matching suggestion review failed', requestError);
      const status = requestStatus(requestError);
      return fail(status === 401 || status === 403 ? status : status === 409 ? 409 : 400, {
        actionError:
          status === 409
            ? 'This suggestion changed. Refresh before reviewing it again.'
            : 'The suggestion review could not be saved.'
      });
    }
  },

  publishPolicy: async ({ request, cookies, locals }) => {
    const access = await requireCapability(cookies, locals, 'calibrate');
    if (access.failure) return access.failure;
    const form = await request.formData();
    const versionId = String(form.get('version_id') || '');
    const revision = expectedRevision(form.get('expected_revision'));
    const idempotencyKey = String(form.get('idempotency_key') || '');
    if (!isUuid(versionId) || revision === null || !isUuid(idempotencyKey)) {
      return fail(400, { actionError: 'The policy publication is stale or invalid.' });
    }
    try {
      const result = await apiRequest(
        `/matching/scoring-policy-versions/${versionId}/publish/`,
        {
          method: 'POST',
          headers: { 'Idempotency-Key': idempotencyKey },
          body: { expected_revision: revision, reason_code: 'administrator_approved' }
        },
        { cookies, org: locals.org }
      );
      const version = normalizeWeightVersion(result?.version ?? result);
      if (!version.id) {
        return fail(502, { actionError: 'The published policy could not be verified safely.' });
      }
      return { feedbackUpdated: true, action: 'publishPolicy', version };
    } catch (requestError) {
      logSafeServerError('Matching policy publication failed', requestError);
      const status = requestStatus(requestError);
      return fail(status === 401 || status === 403 ? status : status === 409 ? 409 : 400, {
        actionError:
          status === 409
            ? 'The scoring policy changed. Review the latest version before publishing.'
            : 'The scoring policy could not be published.'
      });
    }
  }
};
