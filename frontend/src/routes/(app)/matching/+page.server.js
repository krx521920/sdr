import { error, fail } from '@sveltejs/kit';
import { randomUUID } from 'node:crypto';

import { apiRequest } from '$lib/api-helpers.js';
import { REJECTION_REASON_CODES } from '$lib/matching/feedback.js';
import {
  buildCreateOpportunityPayload,
  buildOpportunityQuery,
  chooseOpportunity,
  filterOpportunities,
  isDecisionStatus,
  isUuid,
  normalizeMatch,
  normalizeMatchRun,
  normalizeMatchingCapabilities,
  normalizeOpportunity,
  parseWorkbenchFilters
} from '$lib/matching/workbench.js';
import { logSafeServerError } from '$lib/server/safe-error-log.js';

const DECISION_REASON_CODES = new Set([
  'needs_review',
  'strong_fit',
  'approved',
  ...REJECTION_REASON_CODES
]);

function requireOrg(locals) {
  if (!locals.user?.id || !locals.org?.id) {
    throw error(401, 'Organization context required');
  }
}

/** @param {import('@sveltejs/kit').Cookies} cookies @param {App.Locals} locals */
async function getMatchingPermissions(cookies, locals) {
  return normalizeMatchingCapabilities(
    await apiRequest('/matching/capabilities/', {}, { cookies, org: locals.org })
  );
}

/** @param {unknown} requestError */
function requestStatus(requestError) {
  if (!(requestError instanceof Error)) return 0;
  const directStatus = Number(/** @type {Error & { status?: number }} */ (requestError).status);
  if (Number.isInteger(directStatus) && directStatus >= 100 && directStatus <= 599) {
    return directStatus;
  }
  const match = /^HTTP (\d{3})\b/.exec(requestError.message);
  return match ? Number(match[1]) : 0;
}

/** @type {import('./$types').PageServerLoad} */
export async function load({ cookies, locals, url }) {
  requireOrg(locals);
  const filters = parseWorkbenchFilters(url.searchParams);
  let permissions;

  try {
    permissions = await getMatchingPermissions(cookies, locals);
  } catch (requestError) {
    const status = requestStatus(requestError);
    if (status === 401 || status === 403) {
      throw error(status, 'Matching access is required to use the matching workbench');
    }
    logSafeServerError('Matching capability load failed', requestError);
    throw error(502, 'Matching permissions could not be loaded');
  }
  if (!permissions.read) {
    throw error(403, 'Matching read access is required to use the matching workbench');
  }

  try {
    const [opportunityResponse, peopleResponse] = await Promise.all([
      apiRequest(
        `/matching/opportunities/?${buildOpportunityQuery(filters)}`,
        {},
        { cookies, org: locals.org }
      ),
      apiRequest('/matching/people/?status=active&limit=1', {}, { cookies, org: locals.org }).catch(
        (requestError) => {
          logSafeServerError('Matching active person count load failed', requestError);
          return { count: 0 };
        }
      )
    ]);
    const allOpportunities = Array.isArray(opportunityResponse?.results)
      ? opportunityResponse.results.map(normalizeOpportunity)
      : [];
    const opportunities = filterOpportunities(allOpportunities, filters.q);
    const selectedOpportunity = chooseOpportunity(opportunities, filters.opportunity);

    let matches = [];
    let totalMatches = 0;
    let runHistory = [];
    let currentRun = null;
    if (selectedOpportunity) {
      const matchParams = new URLSearchParams({ limit: '500' });
      if (filters.matchStatus) matchParams.set('status', filters.matchStatus);
      const [matchResponse, runHistoryResponse] = await Promise.all([
        apiRequest(
          `/matching/opportunities/${selectedOpportunity.id}/matches/?${matchParams}`,
          {},
          { cookies, org: locals.org }
        ),
        apiRequest(
          `/matching/opportunities/${selectedOpportunity.id}/match-runs/?limit=10`,
          {},
          { cookies, org: locals.org }
        ).catch((requestError) => {
          logSafeServerError('Matching run history load failed', requestError);
          return { results: [] };
        })
      ]);
      totalMatches = Number(matchResponse?.count) || 0;
      matches = Array.isArray(matchResponse?.results)
        ? matchResponse.results.map(normalizeMatch)
        : [];
      runHistory = Array.isArray(runHistoryResponse?.results)
        ? runHistoryResponse.results.map(normalizeMatchRun).filter((run) => run.id)
        : [];

      currentRun =
        runHistory.find((run) => run.id === filters.run) ||
        runHistory.find((run) =>
          ['pending', 'queued', 'running', 'retry_scheduled'].includes(run.status)
        ) ||
        runHistory[0] ||
        null;

      if (filters.run && currentRun?.id !== filters.run) {
        try {
          const requestedRun = normalizeMatchRun(
            await apiRequest(
              `/matching/match-runs/${filters.run}/`,
              {},
              { cookies, org: locals.org }
            )
          );
          if (requestedRun.id && requestedRun.opportunityId === selectedOpportunity.id) {
            currentRun = requestedRun;
          }
        } catch (requestError) {
          logSafeServerError('Requested matching run load failed', requestError);
        }
      }
    }

    return {
      opportunities,
      opportunityCount: Number(opportunityResponse?.count) || opportunities.length,
      activePersonCount: Number(peopleResponse?.count) || 0,
      selectedOpportunity,
      matches,
      totalMatches,
      runHistory,
      currentRun,
      permissions,
      filters,
      counts: {
        proposed: matches.filter((match) => match.status === 'proposed').length,
        shortlisted: matches.filter((match) => match.status === 'shortlisted').length,
        accepted: matches.filter((match) => match.status === 'accepted').length
      }
    };
  } catch (requestError) {
    logSafeServerError('Matching workbench load failed', requestError);
    const status = requestStatus(requestError);
    if (status === 401 || status === 403) {
      throw error(status, 'Sales access is required to use the matching workbench');
    }
    throw error(502, 'The matching workbench could not be loaded');
  }
}

/** @type {import('./$types').Actions} */
export const actions = {
  onboardPerson: async ({ request, cookies, locals }) => {
    requireOrg(locals);
    let permissions;
    try {
      permissions = await getMatchingPermissions(cookies, locals);
    } catch (requestError) {
      logSafeServerError('Matching person onboarding permission check failed', requestError);
      const status = requestStatus(requestError);
      return fail(status === 401 || status === 403 ? status : 503, {
        actionError: 'Person onboarding permission could not be verified.'
      });
    }
    if (!permissions.manage) {
      return fail(403, { actionError: 'You do not have permission to add people.' });
    }

    const form = await request.formData();
    const rawPayload = String(form.get('payload') || '');
    const idempotencyKey = String(form.get('idempotency_key') || '');
    if (!rawPayload || Buffer.byteLength(rawPayload, 'utf8') > 256 * 1024) {
      return fail(400, { actionError: 'The person onboarding form is invalid.' });
    }
    if (!isUuid(idempotencyKey)) {
      return fail(400, { actionError: 'The onboarding request could not be identified safely.' });
    }

    let payload;
    try {
      payload = JSON.parse(rawPayload);
    } catch {
      return fail(400, { actionError: 'The person onboarding form is invalid.' });
    }
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
      return fail(400, { actionError: 'The person onboarding form is invalid.' });
    }

    try {
      const created = await apiRequest(
        '/matching/people/onboard/',
        {
          method: 'POST',
          headers: { 'Idempotency-Key': idempotencyKey },
          body: payload
        },
        { cookies, org: locals.org }
      );
      const personId = String(created?.person_id || '');
      const displayName =
        typeof payload?.person?.display_name === 'string'
          ? payload.person.display_name.trim().slice(0, 255)
          : '';
      if (!isUuid(personId) || !displayName) {
        return fail(502, { actionError: 'The created person could not be verified.' });
      }
      return {
        onboardingCreated: true,
        onboardedPerson: {
          id: personId,
          displayName,
          replayed: created?.replayed === true
        }
      };
    } catch (requestError) {
      logSafeServerError('Matching person onboarding failed', requestError);
      const status = requestStatus(requestError);
      if (status === 401 || status === 403) {
        return fail(status, { actionError: 'You do not have permission to add people.' });
      }
      if (status === 400 || status === 422) {
        return fail(status, {
          actionError: 'The person could not be added. Review the form and try again.'
        });
      }
      if (status === 409) {
        return fail(409, {
          actionError:
            'This identity is already in use, or this request conflicts with an earlier attempt.'
        });
      }
      if (status === 429) {
        return fail(429, {
          actionError: 'Person onboarding is temporarily rate limited. Try again shortly.'
        });
      }
      return fail(503, {
        actionError: 'Person onboarding is temporarily unavailable. Try again shortly.'
      });
    }
  },

  createOpportunity: async ({ request, cookies, locals }) => {
    requireOrg(locals);
    let permissions;
    try {
      permissions = await getMatchingPermissions(cookies, locals);
    } catch (requestError) {
      logSafeServerError('Matching opportunity permission check failed', requestError);
      const status = requestStatus(requestError);
      return fail(status === 401 || status === 403 ? status : 503, {
        actionError: 'Opportunity permission could not be verified.'
      });
    }
    if (!permissions.manage) {
      return fail(403, { actionError: 'You do not have permission to create opportunities.' });
    }

    const form = await request.formData();
    const parsed = buildCreateOpportunityPayload(form);
    if (!parsed.payload) {
      return fail(400, { actionError: parsed.error || 'The opportunity form is invalid.' });
    }

    try {
      const created = normalizeOpportunity(
        await apiRequest(
          '/matching/opportunities/',
          { method: 'POST', body: parsed.payload },
          { cookies, org: locals.org }
        )
      );
      if (!isUuid(created.id)) {
        return fail(502, { actionError: 'The created opportunity could not be verified.' });
      }
      return {
        opportunityCreated: true,
        createdOpportunity: created
      };
    } catch (requestError) {
      logSafeServerError('Matching opportunity creation failed', requestError);
      const status = requestStatus(requestError);
      if (status === 401 || status === 403) {
        return fail(status, {
          actionError: 'You do not have permission to create opportunities.'
        });
      }
      if (status === 400 || status === 422) {
        return fail(status, {
          actionError: 'The opportunity could not be created. Review the form and try again.'
        });
      }
      if (status === 409) {
        return fail(409, {
          actionError: 'This opportunity conflicts with a recent change. Refresh and try again.'
        });
      }
      if (status === 429) {
        return fail(429, {
          actionError: 'Opportunity creation is temporarily rate limited. Try again shortly.'
        });
      }
      return fail(503, {
        actionError: 'Opportunity creation is temporarily unavailable. Try again shortly.'
      });
    }
  },

  recompute: async ({ request, cookies, locals }) => {
    requireOrg(locals);
    let permissions;
    try {
      permissions = await getMatchingPermissions(cookies, locals);
    } catch (requestError) {
      logSafeServerError('Matching recompute permission check failed', requestError);
      const status = requestStatus(requestError);
      return fail(status === 401 || status === 403 ? status : 503, {
        actionError: 'Recompute permission could not be verified.'
      });
    }
    if (!permissions.recompute) {
      return fail(403, { actionError: 'You do not have permission to recompute matches.' });
    }
    const form = await request.formData();
    const opportunityId = String(form.get('opportunity_id') || '');
    const personId = String(form.get('person_id') || '');
    if (!isUuid(opportunityId)) {
      return fail(400, { actionError: 'Select a valid matching opportunity.' });
    }
    if (personId && !isUuid(personId)) {
      return fail(400, { actionError: 'Select a valid person to rank.' });
    }

    try {
      const submittedKey = String(form.get('idempotency_key') || '');
      const idempotencyKey = isUuid(submittedKey) ? submittedKey : randomUUID();
      const body = {
        idempotency_key: idempotencyKey,
        ...(personId ? { person_ids: [personId] } : {})
      };
      const result = await apiRequest(
        `/matching/opportunities/${opportunityId}/recompute/`,
        {
          method: 'POST',
          headers: { 'Idempotency-Key': idempotencyKey },
          body
        },
        { cookies, org: locals.org }
      );
      const run = normalizeMatchRun(result);
      if (!run.id || run.opportunityId !== opportunityId) {
        return fail(502, { actionError: 'The recompute run could not be started safely.' });
      }
      return {
        recomputeQueued: true,
        run,
        focusPersonId: personId || null
      };
    } catch (requestError) {
      logSafeServerError('Matching recompute failed', requestError);
      const status = requestStatus(requestError);
      return fail(status === 401 || status === 403 ? status : 400, {
        actionError:
          status === 401 || status === 403
            ? 'You do not have permission to recompute matches.'
            : 'Candidates could not be recomputed. Try again.'
      });
    }
  },

  setStatus: async ({ request, cookies, locals }) => {
    requireOrg(locals);
    let permissions;
    try {
      permissions = await getMatchingPermissions(cookies, locals);
    } catch (requestError) {
      logSafeServerError('Matching decision permission check failed', requestError);
      const status = requestStatus(requestError);
      return fail(status === 401 || status === 403 ? status : 503, {
        actionError: 'Decision permission could not be verified.'
      });
    }
    if (!permissions.decide) {
      return fail(403, { actionError: 'You do not have permission to decide matches.' });
    }
    const form = await request.formData();
    const matchId = String(form.get('match_id') || '');
    const status = String(form.get('status') || '');
    const reasonCode = String(form.get('reason_code') || '');
    const reason = String(form.get('reason') || '')
      .trim()
      .slice(0, 1000);
    const expectedRevision = Number(form.get('expected_revision'));
    const expectedRankingRevision = Number(form.get('expected_ranking_revision'));
    if (
      !isUuid(matchId) ||
      !isDecisionStatus(status) ||
      !DECISION_REASON_CODES.has(reasonCode) ||
      !Number.isSafeInteger(expectedRevision) ||
      expectedRevision < 0 ||
      !Number.isSafeInteger(expectedRankingRevision) ||
      expectedRankingRevision < 0
    ) {
      return fail(400, { actionError: 'The match decision is invalid.' });
    }

    try {
      const submittedKey = String(form.get('idempotency_key') || '');
      const idempotencyKey = isUuid(submittedKey) ? submittedKey : randomUUID();
      const updated = await apiRequest(
        `/matching/matches/${matchId}/`,
        {
          method: 'PATCH',
          headers: { 'Idempotency-Key': idempotencyKey },
          body: {
            status,
            reason_code: reasonCode,
            reason,
            expected_revision: expectedRevision,
            expected_ranking_revision: expectedRankingRevision,
            idempotency_key: idempotencyKey
          }
        },
        { cookies, org: locals.org }
      );
      return {
        matchUpdated: true,
        updatedMatch: normalizeMatch(updated)
      };
    } catch (requestError) {
      logSafeServerError('Matching decision update failed', requestError);
      const requestErrorStatus = requestStatus(requestError);
      if (requestErrorStatus === 409) {
        return fail(409, {
          conflict: true,
          actionError:
            'This candidate changed while you were reviewing it. Review the latest version and try again.'
        });
      }
      return fail(
        requestErrorStatus === 401 || requestErrorStatus === 403 ? requestErrorStatus : 400,
        {
          actionError:
            requestErrorStatus === 401 || requestErrorStatus === 403
              ? 'You do not have permission to decide matches.'
              : 'The match decision could not be saved. Try again.'
        }
      );
    }
  }
};
