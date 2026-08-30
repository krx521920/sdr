import { error } from '@sveltejs/kit';

import { apiRequest } from '$lib/api-helpers.js';
import { isGovernanceUuid } from '$lib/matching/governance.js';
import { normalizeMatchingCapabilities } from '$lib/matching/workbench.js';
import { logSafeServerError } from '$lib/server/safe-error-log.js';

const MAX_EXPORT_BYTES = 10 * 1024 * 1024;

/** @param {unknown} requestError */
function requestStatus(requestError) {
  const status = Number(/** @type {{ status?: number }} */ (requestError)?.status);
  return Number.isInteger(status) && status >= 100 && status <= 599 ? status : 0;
}

/** @type {import('./$types').RequestHandler} */
export async function POST({ params, request, cookies, locals }) {
  if (!locals.user?.id || !locals.org?.id) throw error(401, 'Organization context required');
  if (!isGovernanceUuid(params.personId)) throw error(404, 'Governance person not found');
  const form = await request.formData();
  const revision = Number(form.get('expected_revision'));
  const idempotencyKey = String(form.get('idempotency_key') || '');
  if (!Number.isSafeInteger(revision) || revision < 0 || !isGovernanceUuid(idempotencyKey)) {
    throw error(400, 'The export request is stale or invalid');
  }

  try {
    const permissions = normalizeMatchingCapabilities(
      await apiRequest('/matching/capabilities/', {}, { cookies, org: locals.org })
    );
    if (!permissions.export) {
      throw error(403, 'Only an organization administrator can export matching data');
    }
    const exportData = await apiRequest(
      `/matching/people/${params.personId}/export/`,
      {
        method: 'POST',
        headers: { 'Idempotency-Key': idempotencyKey },
        body: {
          expected_revision: revision,
          idempotency_key: idempotencyKey
        }
      },
      { cookies, org: locals.org }
    );
    const safeExport =
      exportData && typeof exportData === 'object' && !Array.isArray(exportData)
        ? /** @type {Record<string, unknown>} */ (exportData).export
        : null;
    if (!safeExport || typeof safeExport !== 'object' || Array.isArray(safeExport)) {
      throw error(502, 'The export could not be verified safely');
    }
    const body = JSON.stringify(safeExport, null, 2);
    if (new TextEncoder().encode(body).byteLength > MAX_EXPORT_BYTES) {
      throw error(502, 'The export is too large to download safely');
    }
    return new Response(body, {
      headers: {
        'Cache-Control': 'no-store, private',
        'Content-Disposition': `attachment; filename="matching-person-${params.personId}.json"`,
        'Content-Type': 'application/json; charset=utf-8',
        'X-Content-Type-Options': 'nosniff'
      }
    });
  } catch (requestError) {
    logSafeServerError('Matching person export failed', requestError);
    const status = requestStatus(requestError);
    throw error(
      status === 400 || status === 401 || status === 403 || status === 404 || status === 409
        ? status
        : 502,
      status === 403
        ? 'Only an organization administrator can export matching data.'
        : status === 404
          ? 'Governance person not found.'
          : status === 409
            ? 'Matching data changed. Refresh before exporting.'
            : 'The matching-owned export could not be created.'
    );
  }
}
