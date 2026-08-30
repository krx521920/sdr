import { error, fail } from '@sveltejs/kit';

import { apiRequest } from '$lib/api-helpers.js';
import {
  isUuid,
  normalizeImportBatch,
  normalizeImportRecord,
  normalizeImportRecordList
} from '$lib/matching/imports.js';
import { normalizeMatchingCapabilities } from '$lib/matching/workbench.js';
import { logSafeServerError } from '$lib/server/safe-error-log.js';

const RECORD_STATUSES = new Set([
  'ready',
  'invalid',
  'created',
  'merged',
  'conflict',
  'skipped',
  'replayed',
  'failed'
]);
const RESOLUTION_ACTIONS = new Set(['link_existing', 'skip']);

/** @param {unknown} requestError */
function requestStatus(requestError) {
  const status = Number(/** @type {{ status?: number }} */ (requestError)?.status);
  return Number.isInteger(status) && status >= 100 && status <= 599 ? status : 0;
}

/** @param {import('@sveltejs/kit').Cookies} cookies @param {App.Locals} locals */
async function matchingCapabilities(cookies, locals) {
  return normalizeMatchingCapabilities(
    await apiRequest('/matching/capabilities/', {}, { cookies, org: locals.org })
  );
}

/** @param {unknown} value */
function expectedRevision(value) {
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed >= 0 ? parsed : null;
}

/** @type {import('./$types').PageServerLoad} */
export async function load({ params, cookies, locals, url }) {
  if (!locals.user?.id || !locals.org?.id) throw error(401, 'Organization context required');
  if (!isUuid(params.batchId)) throw error(404, 'Import batch not found');

  let permissions;
  try {
    permissions = await matchingCapabilities(cookies, locals);
  } catch (requestError) {
    logSafeServerError('Matching import detail capability load failed', requestError);
    const status = requestStatus(requestError);
    throw error(
      status === 401 || status === 403 ? status : 502,
      'Import access could not be verified'
    );
  }
  if (!permissions.manage) throw error(403, 'Matching manage access is required to view imports');

  const requestedStatus = String(url.searchParams.get('status') || '').toLowerCase();
  const recordStatus = RECORD_STATUSES.has(requestedStatus) ? requestedStatus : '';
  const opportunity = String(url.searchParams.get('opportunity') || '');
  try {
    const query = new URLSearchParams({ limit: '500' });
    if (recordStatus) query.set('status', recordStatus);
    const [batchResult, recordResult] = await Promise.all([
      apiRequest(`/matching/person-imports/${params.batchId}/`, {}, { cookies, org: locals.org }),
      apiRequest(
        `/matching/person-imports/${params.batchId}/records/?${query}`,
        {},
        { cookies, org: locals.org }
      )
    ]);
    const batch = normalizeImportBatch(batchResult);
    if (!batch.id) throw error(404, 'Import batch not found');
    return {
      permissions,
      batch,
      records: normalizeImportRecordList(recordResult),
      recordCount: Number(recordResult?.count) || 0,
      recordStatus,
      opportunity: isUuid(opportunity) ? opportunity : ''
    };
  } catch (requestError) {
    if (/** @type {{ status?: number }} */ (requestError)?.status === 404) throw requestError;
    logSafeServerError('Matching import detail load failed', requestError);
    const status = requestStatus(requestError);
    throw error(
      status === 401 || status === 403 || status === 404 ? status : 502,
      status === 404 ? 'Import batch not found' : 'The import batch could not be loaded'
    );
  }
}

/** @type {import('./$types').Actions} */
export const actions = {
  commit: async ({ params, request, cookies, locals }) => {
    if (!locals.user?.id || !locals.org?.id) {
      return fail(401, { actionError: 'Organization context is required.' });
    }
    if (!isUuid(params.batchId)) return fail(404, { actionError: 'Import batch not found.' });
    try {
      const permissions = await matchingCapabilities(cookies, locals);
      if (!permissions.manage) {
        return fail(403, { actionError: 'You no longer have permission to import people.' });
      }
    } catch (requestError) {
      logSafeServerError('Matching import commit permission check failed', requestError);
      const status = requestStatus(requestError);
      return fail(status === 401 || status === 403 ? status : 503, {
        actionError: 'Import permission could not be verified.'
      });
    }

    const form = await request.formData();
    const revision = expectedRevision(form.get('expected_revision'));
    const idempotencyKey = String(form.get('idempotency_key') || '');
    if (revision === null || !isUuid(idempotencyKey)) {
      return fail(400, { actionError: 'The commit request is stale or invalid.' });
    }
    try {
      const result = await apiRequest(
        `/matching/person-imports/${params.batchId}/commit/`,
        {
          method: 'POST',
          headers: { 'Idempotency-Key': idempotencyKey },
          body: { expected_revision: revision }
        },
        { cookies, org: locals.org }
      );
      const batch = normalizeImportBatch(result);
      if (!batch.id) {
        return fail(502, { actionError: 'The queued import could not be verified safely.' });
      }
      return { commitQueued: true, batch };
    } catch (requestError) {
      logSafeServerError('Matching import commit failed', requestError);
      const status = requestStatus(requestError);
      return fail(status === 401 || status === 403 ? status : status === 409 ? 409 : 400, {
        actionError:
          status === 409
            ? 'This batch changed. Refresh it before committing.'
            : status === 403
              ? 'You no longer have permission to import people.'
              : 'The import could not be queued. Resolve blocking rows and try again.'
      });
    }
  },

  resolve: async ({ request, cookies, locals }) => {
    if (!locals.user?.id || !locals.org?.id) {
      return fail(401, { actionError: 'Organization context is required.' });
    }
    try {
      const permissions = await matchingCapabilities(cookies, locals);
      if (!permissions.manage) {
        return fail(403, { actionError: 'You no longer have permission to review conflicts.' });
      }
    } catch (requestError) {
      logSafeServerError('Matching import resolution permission check failed', requestError);
      const status = requestStatus(requestError);
      return fail(status === 401 || status === 403 ? status : 503, {
        actionError: 'Conflict review permission could not be verified.'
      });
    }

    const form = await request.formData();
    const recordId = String(form.get('record_id') || '');
    const action = String(form.get('resolution') || '');
    const personId = String(form.get('person_id') || '');
    const revision = expectedRevision(form.get('expected_revision'));
    const idempotencyKey = String(form.get('idempotency_key') || '');
    if (
      !isUuid(recordId) ||
      !RESOLUTION_ACTIONS.has(action) ||
      revision === null ||
      !isUuid(idempotencyKey) ||
      (action === 'link_existing' && !isUuid(personId))
    ) {
      return fail(400, { actionError: 'Choose a valid conflict resolution.' });
    }
    try {
      const result = await apiRequest(
        `/matching/person-import-records/${recordId}/resolve/`,
        {
          method: 'POST',
          headers: { 'Idempotency-Key': idempotencyKey },
          body: {
            action,
            expected_revision: revision,
            ...(action === 'link_existing' ? { person_id: personId } : {})
          }
        },
        { cookies, org: locals.org }
      );
      const record = normalizeImportRecord(result?.record ?? result);
      return { conflictResolved: true, recordId, record };
    } catch (requestError) {
      logSafeServerError('Matching import conflict resolution failed', requestError);
      const status = requestStatus(requestError);
      return fail(status === 401 || status === 403 ? status : status === 409 ? 409 : 400, {
        actionError:
          status === 409
            ? 'This conflict changed. Refresh before choosing again.'
            : status === 403
              ? 'You no longer have permission to review conflicts.'
              : 'The conflict could not be resolved.'
      });
    }
  }
};
