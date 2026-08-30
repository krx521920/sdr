import { json } from '@sveltejs/kit';

import { apiRequest } from '$lib/api-helpers.js';
import { isUuid, normalizeMatchRun } from '$lib/matching/workbench.js';
import { logSafeServerError } from '$lib/server/safe-error-log.js';

/** @param {unknown} requestError */
function requestStatus(requestError) {
  if (!(requestError instanceof Error)) return 0;
  const status = Number(/** @type {Error & { status?: number }} */ (requestError).status);
  return Number.isInteger(status) && status >= 100 && status <= 599 ? status : 0;
}

/** @type {import('./$types').RequestHandler} */
export async function GET({ url, cookies, locals }) {
  const runId = String(url.searchParams.get('run') || '');
  const opportunityId = String(url.searchParams.get('opportunity') || '');
  if (!locals.user?.id || !locals.org?.id) {
    return json({ run: null, runs: [], error: 'AUTH_REQUIRED' }, { status: 401 });
  }
  if (!isUuid(runId) || !isUuid(opportunityId)) {
    return json({ run: null, runs: [], error: 'INVALID_RUN_REFERENCE' }, { status: 400 });
  }

  const [runResult, historyResult] = await Promise.allSettled([
    apiRequest(`/matching/match-runs/${runId}/`, {}, { cookies, org: locals.org }),
    apiRequest(
      `/matching/opportunities/${opportunityId}/match-runs/?limit=10`,
      {},
      { cookies, org: locals.org }
    )
  ]);

  if (runResult.status === 'rejected') {
    logSafeServerError('Matching run poll failed', runResult.reason);
    const status = requestStatus(runResult.reason);
    return json(
      { run: null, runs: [], error: status === 404 ? 'RUN_NOT_FOUND' : 'RUN_STATUS_UNAVAILABLE' },
      { status: status === 401 || status === 403 || status === 404 ? status : 502 }
    );
  }

  const run = normalizeMatchRun(runResult.value);
  if (!run.id || run.opportunityId !== opportunityId) {
    return json({ run: null, runs: [], error: 'RUN_NOT_FOUND' }, { status: 404 });
  }

  let runs = [];
  if (historyResult.status === 'fulfilled' && Array.isArray(historyResult.value?.results)) {
    runs = historyResult.value.results.map(normalizeMatchRun).filter((item) => item.id);
  } else if (historyResult.status === 'rejected') {
    logSafeServerError('Matching run history poll failed', historyResult.reason);
  }

  return json({ run, runs });
}
