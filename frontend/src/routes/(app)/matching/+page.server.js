import { error, fail } from '@sveltejs/kit';

import { apiRequest } from '$lib/api-helpers.js';
import {
  buildOpportunityQuery,
  chooseOpportunity,
  filterOpportunities,
  isDecisionStatus,
  isUuid,
  normalizeMatch,
  normalizeOpportunity,
  parseWorkbenchFilters
} from '$lib/matching/workbench.js';
import { logSafeServerError } from '$lib/server/safe-error-log.js';

function requireOrg(locals) {
  if (!locals.user?.id || !locals.org?.id) {
    throw error(401, 'Organization context required');
  }
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

  try {
    const opportunityResponse = await apiRequest(
      `/matching/opportunities/?${buildOpportunityQuery(filters)}`,
      {},
      { cookies, org: locals.org }
    );
    const allOpportunities = Array.isArray(opportunityResponse?.results)
      ? opportunityResponse.results.map(normalizeOpportunity)
      : [];
    const opportunities = filterOpportunities(allOpportunities, filters.q);
    const selectedOpportunity = chooseOpportunity(opportunities, filters.opportunity);

    let matches = [];
    let totalMatches = 0;
    if (selectedOpportunity) {
      const matchParams = new URLSearchParams({ limit: '500' });
      if (filters.matchStatus) matchParams.set('status', filters.matchStatus);
      const matchResponse = await apiRequest(
        `/matching/opportunities/${selectedOpportunity.id}/matches/?${matchParams}`,
        {},
        { cookies, org: locals.org }
      );
      totalMatches = Number(matchResponse?.count) || 0;
      matches = Array.isArray(matchResponse?.results)
        ? matchResponse.results.map(normalizeMatch)
        : [];
    }

    return {
      opportunities,
      opportunityCount: Number(opportunityResponse?.count) || opportunities.length,
      selectedOpportunity,
      matches,
      totalMatches,
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
  recompute: async ({ request, cookies, locals }) => {
    requireOrg(locals);
    const form = await request.formData();
    const opportunityId = String(form.get('opportunity_id') || '');
    if (!isUuid(opportunityId)) {
      return fail(400, { actionError: 'Select a valid matching opportunity.' });
    }

    try {
      const result = await apiRequest(
        `/matching/opportunities/${opportunityId}/matches/`,
        { method: 'POST', body: {} },
        { cookies, org: locals.org }
      );
      return {
        recomputed: true,
        recomputedCount: Number(result?.count) || 0
      };
    } catch (requestError) {
      logSafeServerError('Matching recompute failed', requestError);
      const status = requestStatus(requestError);
      return fail(status === 401 || status === 403 ? status : 400, {
        actionError:
          status === 401 || status === 403
            ? 'Sales access is required to recompute matches.'
            : 'Candidates could not be recomputed. Try again.'
      });
    }
  },

  setStatus: async ({ request, cookies, locals }) => {
    requireOrg(locals);
    const form = await request.formData();
    const matchId = String(form.get('match_id') || '');
    const status = String(form.get('status') || '');
    if (!isUuid(matchId) || !isDecisionStatus(status)) {
      return fail(400, { actionError: 'The match decision is invalid.' });
    }

    try {
      const updated = await apiRequest(
        `/matching/matches/${matchId}/`,
        { method: 'PATCH', body: { status } },
        { cookies, org: locals.org }
      );
      return {
        matchUpdated: true,
        updatedMatch: normalizeMatch(updated)
      };
    } catch (requestError) {
      logSafeServerError('Matching decision update failed', requestError);
      const requestErrorStatus = requestStatus(requestError);
      return fail(
        requestErrorStatus === 401 || requestErrorStatus === 403 ? requestErrorStatus : 400,
        {
          actionError:
            requestErrorStatus === 401 || requestErrorStatus === 403
              ? 'Sales access is required to update match decisions.'
              : 'The match decision could not be saved. Try again.'
        }
      );
    }
  }
};
