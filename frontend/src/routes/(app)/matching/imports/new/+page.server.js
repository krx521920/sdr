import { error, fail } from '@sveltejs/kit';

import { apiMultipartRequest, apiRequest } from '$lib/api-helpers.js';
import {
  buildFeishuPersonImportExecution,
  buildFeishuPersonImportPreview,
  normalizeFeishuPersonImportConnection,
  normalizeFeishuPersonImportExecution,
  normalizeFeishuPersonImportIntent,
  validFeishuImportUuid,
  validateFeishuPersonImportMapping
} from '$lib/feishu-base-import.js';
import {
  isUuid,
  MATCHING_CRM_ENTITY_TYPES,
  MATCHING_CRM_PAGE_SIZE,
  MATCHING_IMPORT_MAX_BYTES,
  normalizeCrmImportCandidateList,
  normalizeImportBatch,
  parseCsvHeaders,
  validateCrmImportSelection,
  validateImportMapping
} from '$lib/matching/imports.js';
import { normalizeMatchingCapabilities } from '$lib/matching/workbench.js';
import { logSafeServerError } from '$lib/server/safe-error-log.js';

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

/** @param {FormData} form */
function feishuImportPayload(form) {
  let mapping;
  try {
    mapping = JSON.parse(String(form.get('mapping') || ''));
  } catch {
    return { payload: null, error: 'Configure the Feishu field mapping before continuing.' };
  }
  const validated = validateFeishuPersonImportMapping(mapping);
  if (!validated.mapping) return { payload: null, error: validated.error };
  const payload = buildFeishuPersonImportPreview(validated.mapping, form.get('limit'));
  return {
    payload,
    error: payload ? '' : 'Choose a record limit between 1 and 500.'
  };
}

/**
 * Re-check both matching access and the integrations-admin boundary for every
 * provider action. The page selection is never treated as authorization.
 * @param {import('@sveltejs/kit').Cookies} cookies
 * @param {App.Locals} locals
 */
async function requireFeishuImportAccess(cookies, locals) {
  if (!locals.user?.id || !locals.org?.id) {
    return {
      permissions: null,
      failure: fail(401, { actionError: 'Organization context is required.' })
    };
  }
  try {
    const permissions = await matchingCapabilities(cookies, locals);
    if (!permissions.manage || !permissions.calibrate) {
      return {
        permissions,
        failure: fail(403, {
          actionError: 'Organization admin access is required for Feishu imports.'
        })
      };
    }
    return { permissions, failure: null };
  } catch (requestError) {
    logSafeServerError('Feishu import permission check failed', requestError);
    const status = requestStatus(requestError);
    return {
      permissions: null,
      failure: fail(status === 401 || status === 403 ? status : 503, {
        actionError: 'Import permission could not be verified.'
      })
    };
  }
}

/** @type {import('./$types').PageServerLoad} */
export async function load({ cookies, locals, url }) {
  if (!locals.user?.id || !locals.org?.id) throw error(401, 'Organization context required');
  let permissions;
  try {
    permissions = await matchingCapabilities(cookies, locals);
  } catch (requestError) {
    logSafeServerError('Matching import capability load failed', requestError);
    const status = requestStatus(requestError);
    throw error(
      status === 401 || status === 403 ? status : 502,
      'Import access could not be verified'
    );
  }
  if (!permissions.manage) throw error(403, 'Matching manage access is required to import people');
  const opportunity = String(url.searchParams.get('opportunity') || '');
  const requestedSource = String(url.searchParams.get('source') || '');
  const source =
    requestedSource === 'crm'
      ? 'crm'
      : requestedSource === 'feishu'
        ? 'feishu'
        : requestedSource === 'email'
          ? 'email'
          : 'csv';
  if (['feishu', 'email'].includes(source) && !permissions.calibrate) {
    throw error(403, 'Organization admin access is required for this import source');
  }
  const requestedEntityType = String(url.searchParams.get('entity_type') || '').toLowerCase();
  const entityType = MATCHING_CRM_ENTITY_TYPES.includes(requestedEntityType)
    ? requestedEntityType
    : 'lead';
  const search = String(url.searchParams.get('search') || '')
    .trim()
    .slice(0, 100);
  let crmCandidates = { count: 0, results: [] };
  let feishuConnection = { configured: false, active: false };
  let feishuLoadError = '';
  let emailBatches = { count: 0, results: [] };
  let emailLoadError = '';
  if (source === 'crm') {
    const query = new URLSearchParams({
      entity_type: entityType,
      page_size: String(MATCHING_CRM_PAGE_SIZE)
    });
    if (search) query.set('search', search);
    try {
      crmCandidates = normalizeCrmImportCandidateList(
        await apiRequest(
          `/matching/person-imports/crm/candidates/?${query}`,
          {},
          { cookies, org: locals.org }
        )
      );
    } catch (requestError) {
      logSafeServerError('Matching CRM candidate load failed', requestError);
      const status = requestStatus(requestError);
      throw error(
        status === 401 || status === 403 ? status : 502,
        'CRM candidates could not be loaded'
      );
    }
  }
  if (source === 'feishu') {
    try {
      feishuConnection = normalizeFeishuPersonImportConnection(
        await apiRequest('/integrations/feishu-base/connection/', {}, { cookies, org: locals.org })
      );
    } catch (requestError) {
      logSafeServerError('Feishu import connection load failed', requestError);
      const status = requestStatus(requestError);
      if (status === 401 || status === 403) {
        throw error(status, 'Feishu connection access could not be verified');
      }
      feishuLoadError = 'The Feishu Base connection could not be loaded.';
    }
  }
  if (source === 'email') {
    try {
      const response = await apiRequest(
        '/matching/person-imports/?source=email&limit=20',
        {},
        { cookies, org: locals.org }
      );
      const rawResults = Array.isArray(response?.results) ? response.results : [];
      emailBatches = {
        count: Number.isInteger(response?.count) ? Math.max(0, response.count) : 0,
        results: rawResults
          .map(normalizeImportBatch)
          .filter((batch) => batch.id && batch.source === 'email')
          .slice(0, 20)
      };
    } catch (requestError) {
      logSafeServerError('Inbound Email preview list failed', requestError);
      const status = requestStatus(requestError);
      if (status === 401 || status === 403) {
        throw error(status, 'Email import access could not be verified');
      }
      emailLoadError = 'Inbound Email previews could not be loaded.';
    }
  }
  return {
    permissions,
    opportunity: isUuid(opportunity) ? opportunity : '',
    source,
    entityType,
    search,
    crmCandidates,
    feishuConnection,
    feishuLoadError,
    emailBatches,
    emailLoadError
  };
}

/** @type {import('./$types').Actions} */
export const actions = {
  preview: async ({ request, cookies, locals }) => {
    if (!locals.user?.id || !locals.org?.id) {
      return fail(401, { actionError: 'Organization context is required.' });
    }
    let permissions;
    try {
      permissions = await matchingCapabilities(cookies, locals);
    } catch (requestError) {
      logSafeServerError('Matching import permission check failed', requestError);
      const status = requestStatus(requestError);
      return fail(status === 401 || status === 403 ? status : 503, {
        actionError: 'Import permission could not be verified.'
      });
    }
    if (!permissions.manage) {
      return fail(403, { actionError: 'You do not have permission to import people.' });
    }

    const form = await request.formData();
    const file = form.get('file');
    const rawMapping = String(form.get('mapping') || '');
    const idempotencyKey = String(form.get('idempotency_key') || '');
    if (!(file instanceof File) || file.size === 0) {
      return fail(400, { actionError: 'Choose a CSV file.' });
    }
    if (!file.name.toLowerCase().endsWith('.csv') || file.size > MATCHING_IMPORT_MAX_BYTES) {
      return fail(400, { actionError: 'Choose a CSV file no larger than 5 MB.' });
    }
    if (!isUuid(idempotencyKey)) {
      return fail(400, { actionError: 'The import request could not be identified safely.' });
    }

    let mapping;
    try {
      mapping = JSON.parse(rawMapping);
    } catch {
      return fail(400, { actionError: 'Map the CSV columns before continuing.' });
    }
    const headerResult = parseCsvHeaders(await file.slice(0, 64 * 1024).text());
    if (!headerResult.headers) {
      return fail(400, { actionError: headerResult.error || 'The CSV header is invalid.' });
    }
    const mappingResult = validateImportMapping(mapping, headerResult.headers);
    if (!mappingResult.mapping) {
      return fail(400, { actionError: mappingResult.error || 'The CSV mapping is invalid.' });
    }

    const upload = new FormData();
    upload.append('file', file, file.name);
    upload.append('mapping', JSON.stringify(mappingResult.mapping));
    try {
      const result = await apiMultipartRequest(
        '/matching/person-imports/preview/',
        upload,
        { headers: { 'Idempotency-Key': idempotencyKey } },
        { cookies, org: locals.org }
      );
      const batch = normalizeImportBatch(result);
      if (!batch.id) {
        return fail(502, { actionError: 'The import preview could not be verified safely.' });
      }
      return { previewCreated: true, batch };
    } catch (requestError) {
      logSafeServerError('Matching CSV preview failed', requestError);
      const status = requestStatus(requestError);
      return fail(status === 401 || status === 403 ? status : status === 409 ? 409 : 400, {
        actionError:
          status === 403
            ? 'You no longer have permission to import people.'
            : status === 409
              ? 'This preview request conflicts with an earlier upload. Choose the file again.'
              : 'The CSV could not be previewed. Review the file and mapping.'
      });
    }
  },

  crmPreview: async ({ request, cookies, locals }) => {
    if (!locals.user?.id || !locals.org?.id) {
      return fail(401, { actionError: 'Organization context is required.' });
    }
    let permissions;
    try {
      permissions = await matchingCapabilities(cookies, locals);
    } catch (requestError) {
      logSafeServerError('Matching CRM import permission check failed', requestError);
      const status = requestStatus(requestError);
      return fail(status === 401 || status === 403 ? status : 503, {
        actionError: 'Import permission could not be verified.'
      });
    }
    if (!permissions.manage) {
      return fail(403, { actionError: 'You do not have permission to import CRM records.' });
    }

    const form = await request.formData();
    const selection = validateCrmImportSelection(
      form.get('entity_type'),
      form.getAll('record_ids').map(String)
    );
    const idempotencyKey = String(form.get('idempotency_key') || '');
    if (!selection.payload) {
      return fail(400, { actionError: selection.error || 'Select valid CRM records.' });
    }
    if (!isUuid(idempotencyKey)) {
      return fail(400, { actionError: 'The CRM preview request could not be identified safely.' });
    }
    try {
      const result = await apiRequest(
        '/matching/person-imports/crm/preview/',
        {
          method: 'POST',
          headers: { 'Idempotency-Key': idempotencyKey },
          body: selection.payload
        },
        { cookies, org: locals.org }
      );
      const batch = normalizeImportBatch(result);
      if (!batch.id) {
        return fail(502, { actionError: 'The CRM import preview could not be verified safely.' });
      }
      return { previewCreated: true, batch };
    } catch (requestError) {
      logSafeServerError('Matching CRM preview failed', requestError);
      const status = requestStatus(requestError);
      return fail(status === 401 || status === 403 ? status : status === 409 ? 409 : 400, {
        actionError:
          status === 403
            ? 'You no longer have permission to import CRM records.'
            : status === 409
              ? 'This CRM preview conflicts with an earlier request. Review the selection again.'
              : 'The CRM records could not be previewed safely.'
      });
    }
  },

  feishuPrepare: async ({ request, cookies, locals }) => {
    const access = await requireFeishuImportAccess(cookies, locals);
    if (access.failure) return access.failure;
    const parsed = feishuImportPayload(await request.formData());
    if (!parsed.payload) return fail(400, { actionError: parsed.error });
    try {
      const response = await apiRequest(
        '/integrations/feishu-base/person-imports/preview/',
        { method: 'POST', body: parsed.payload },
        { cookies, org: locals.org }
      );
      const intent = normalizeFeishuPersonImportIntent(response);
      if (!intent) {
        return fail(502, {
          actionError: 'The Feishu approval intent could not be verified safely.'
        });
      }
      return { feishuApprovalRequired: true, feishuIntent: intent };
    } catch (requestError) {
      logSafeServerError('Feishu import intent preparation failed', requestError);
      const status = requestStatus(requestError);
      return fail(status === 401 || status === 403 ? status : status === 409 ? 409 : 400, {
        actionError:
          status === 403
            ? 'You no longer have permission to import from Feishu.'
            : status === 409
              ? 'The Feishu import configuration changed. Prepare a new approval intent.'
              : 'The Feishu import intent could not be prepared safely.'
      });
    }
  },

  feishuExecute: async ({ request, cookies, locals }) => {
    const access = await requireFeishuImportAccess(cookies, locals);
    if (access.failure) return access.failure;
    const form = await request.formData();
    const parsed = feishuImportPayload(form);
    if (!parsed.payload) return fail(400, { actionError: parsed.error });
    const execution = buildFeishuPersonImportExecution(
      parsed.payload.mapping,
      parsed.payload.limit,
      form.get('approval_id'),
      // One approval can authorize only this exact request, so reusing its UUID
      // as the execution idempotency key makes a lost HTTP response recoverable
      // without storing provider fields or an extra key in the browser.
      () => String(form.get('approval_id') || '')
    );
    if (!execution) {
      return fail(400, { actionError: 'Enter a valid approval ID from Channel Safety.' });
    }
    try {
      const response = await apiRequest(
        '/integrations/feishu-base/person-imports/preview/',
        { method: 'POST', body: execution },
        { cookies, org: locals.org }
      );
      const importRequest = normalizeFeishuPersonImportExecution(response);
      if (!importRequest) {
        return fail(502, { actionError: 'The queued Feishu import could not be verified safely.' });
      }
      return { feishuImportQueued: true, feishuImport: importRequest };
    } catch (requestError) {
      logSafeServerError('Feishu approved import enqueue failed', requestError);
      const status = requestStatus(requestError);
      return fail(status === 401 || status === 403 ? status : status === 409 ? 409 : 400, {
        actionError:
          status === 403
            ? 'The approval is not valid for this Feishu import.'
            : status === 409
              ? 'The approval was already used or the import intent changed.'
              : 'The approved Feishu import could not be queued safely.'
      });
    }
  },

  feishuStatus: async ({ request, cookies, locals }) => {
    const access = await requireFeishuImportAccess(cookies, locals);
    if (access.failure) return access.failure;
    const form = await request.formData();
    const importId = String(form.get('import_id') || '');
    if (!validFeishuImportUuid(importId)) {
      return fail(400, { actionError: 'The Feishu import status request is invalid.' });
    }
    try {
      const response = await apiRequest(
        `/integrations/feishu-base/person-imports/${importId}/`,
        {},
        { cookies, org: locals.org }
      );
      const importRequest = normalizeFeishuPersonImportExecution(response);
      if (!importRequest || importRequest.id !== importId) {
        return fail(502, { actionError: 'The Feishu import status could not be verified safely.' });
      }
      return { feishuImportStatus: true, feishuImport: importRequest };
    } catch (requestError) {
      logSafeServerError('Feishu import status load failed', requestError);
      const status = requestStatus(requestError);
      return fail(status === 401 || status === 403 || status === 404 ? status : 502, {
        actionError: 'The Feishu import status could not be loaded.'
      });
    }
  }
};
