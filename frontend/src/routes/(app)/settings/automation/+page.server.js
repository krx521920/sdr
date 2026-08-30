import { apiRequest } from '$lib/api-helpers.js';
import {
  FEISHU_BASE_ACTIONS,
  buildFeishuBaseApprovedExecution,
  buildFeishuBaseConnectionWrite,
  normalizeFeishuBaseConnection,
  normalizeFeishuBaseIntent,
  normalizeFeishuBaseSyncs
} from '$lib/feishu-base-execution.js';
import { logSafeServerError } from '$lib/server/safe-error-log.js';
import { error, fail } from '@sveltejs/kit';

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const VALID_STATUSES = new Set([
  'pending',
  'queued',
  'running',
  'retry_scheduled',
  'succeeded',
  'dead_letter',
  'cancelled'
]);
const FEISHU_BASE_MAPPING_KEYS = [
  'intake_id',
  'company_name',
  'contact_name',
  'email',
  'phone',
  'linkedin_url',
  'website',
  'source',
  'source_record_id',
  'research_summary',
  'research_facts',
  'source_urls',
  'qualification_score',
  'qualification_band',
  'qualification_reasons',
  'assigned_sales',
  'routing_reason',
  'crm_lead_id',
  'processed_at',
  'inspection_status'
];

function requireAdmin(profile) {
  if (profile?.role !== 'ADMIN' && profile?.is_organization_admin !== true) {
    throw error(403, 'Administrator access required');
  }
}

function fixedFailure(cause, message) {
  logSafeServerError('Feishu Base operation failed', cause);
  const status = Number(cause?.status);
  return fail(status === 403 ? 403 : status === 409 ? 409 : 400, {
    actionError:
      status === 403
        ? 'Your administrator permission changed.'
        : status === 409
          ? 'Safety state changed. Refresh and review before retrying.'
          : message
  });
}

async function prepareFeishuIntent(endpoint, expectedAction, cookies, org) {
  const response = await apiRequest(endpoint, { method: 'POST' }, { cookies, org });
  return normalizeFeishuBaseIntent(response, expectedAction);
}

async function executeApprovedFeishuAction(endpoint, approvalId, cookies, org) {
  const body = buildFeishuBaseApprovedExecution(approvalId, () => crypto.randomUUID());
  if (!body) return null;
  await apiRequest(endpoint, { method: 'POST', body }, { cookies, org });
  return body;
}

/** @type {import('./$types').PageServerLoad} */
export async function load({ cookies, locals, url }) {
  const requestedStatus = url.searchParams.get('status') || '';
  const status = VALID_STATUSES.has(requestedStatus) ? requestedStatus : '';
  const query = new URLSearchParams({ limit: '50' });
  if (status) query.set('status', status);

  try {
    const [jobs, intakes, responseSettings, feishuBase, feishuSyncs] = await Promise.all([
      apiRequest(`/automation/jobs/?${query}`, {}, { cookies }),
      apiRequest('/sdr/intakes/?limit=50', {}, { cookies }),
      apiRequest('/sdr/response-settings/', {}, { cookies }),
      apiRequest('/integrations/feishu-base/connection/', {}, { cookies, org: locals?.org }),
      apiRequest('/integrations/feishu-base/syncs/', {}, { cookies, org: locals?.org }).catch(
        () => ({ results: [] })
      )
    ]);
    return {
      jobs,
      intakes,
      responseSettings,
      feishuBase: normalizeFeishuBaseConnection(feishuBase),
      feishuSyncs: normalizeFeishuBaseSyncs(feishuSyncs),
      selectedStatus: status,
      loadError: ''
    };
  } catch {
    return {
      jobs: { count: 0, summary: {}, results: [] },
      intakes: {
        count: 0,
        summary: {},
        delivery_summary: {},
        response_metrics: {},
        results: []
      },
      responseSettings: {},
      feishuBase: normalizeFeishuBaseConnection({}),
      feishuSyncs: normalizeFeishuBaseSyncs([]),
      selectedStatus: status,
      loadError: 'Could not load automation jobs.'
    };
  }
}

/** @type {import('./$types').Actions} */
export const actions = {
  retry: async ({ request, cookies, locals }) => {
    requireAdmin(locals.profile);
    const formData = await request.formData();
    const jobId = formData.get('job_id')?.toString() || '';
    if (!UUID_PATTERN.test(jobId)) {
      return fail(400, { actionError: 'The automation job is invalid.' });
    }
    try {
      await apiRequest(`/automation/jobs/${jobId}/retry/`, { method: 'POST' }, { cookies });
      return { retried: true };
    } catch (cause) {
      return fixedFailure(cause, 'Could not retry the automation job.');
    }
  },
  saveResponse: async ({ request, cookies, locals }) => {
    requireAdmin(locals.profile);
    const formData = await request.formData();
    const body = {
      acknowledgement_email_enabled: formData.has('acknowledgement_email_enabled'),
      acknowledgement_subject: String(formData.get('acknowledgement_subject') || '').trim(),
      acknowledgement_body: String(formData.get('acknowledgement_body') || '').trim(),
      acknowledgement_from_email: String(formData.get('acknowledgement_from_email') || '').trim(),
      sales_in_app_enabled: formData.has('sales_in_app_enabled'),
      // The legacy bot webhook has no exact one-time approval contract yet.
      // Keep it fail-closed even if a crafted form submits the old fields.
      feishu_enabled: false,
      feishu_webhook_url: '',
      clear_feishu_webhook: formData.has('clear_feishu_webhook'),
      response_sla_seconds: Number(formData.get('response_sla_seconds') || 60)
    };
    try {
      await apiRequest('/sdr/response-settings/', { method: 'PUT', body }, { cookies });
      return { responseSaved: true };
    } catch (cause) {
      return fixedFailure(cause, 'Could not save lead response settings.');
    }
  },
  saveFeishuBase: async ({ request, cookies, locals }) => {
    requireAdmin(locals.profile);
    const formData = await request.formData();
    const fieldMapping = {};
    for (const key of FEISHU_BASE_MAPPING_KEYS) {
      const fieldName = String(formData.get(`mapping_${key}`) || '').trim();
      if (fieldName.length > 100) {
        return fail(400, { actionError: 'A Feishu Base field name is too long.' });
      }
      if (fieldName) fieldMapping[key] = fieldName;
    }
    const names = Object.values(fieldMapping);
    if (new Set(names).size !== names.length) {
      return fail(400, { actionError: 'Each Feishu Base field may be mapped only once.' });
    }
    const appId = String(formData.get('app_id') || '').trim();
    const appSecret = String(formData.get('app_secret') || '').trim();
    const appToken = String(formData.get('app_token') || '').trim();
    const tableId = String(formData.get('table_id') || '').trim();
    if (
      appId.length > 100 ||
      appSecret.length > 4096 ||
      appToken.length > 255 ||
      tableId.length > 255
    ) {
      return fail(400, { actionError: 'Feishu Base connection values are invalid.' });
    }
    const body = buildFeishuBaseConnectionWrite({
      appId,
      appSecret,
      appToken,
      tableId,
      fieldMapping,
      isActive: formData.has('connection_enabled')
    });
    if (!body) return fail(400, { actionError: 'Feishu Base connection values are invalid.' });
    try {
      await apiRequest(
        '/integrations/feishu-base/connection/',
        { method: 'PUT', body },
        { cookies, org: locals?.org }
      );
      return { feishuBaseSaved: true };
    } catch (cause) {
      return fixedFailure(cause, 'Could not save the Feishu Base connection.');
    }
  },
  prepareFeishuSchemaValidation: async ({ cookies, locals }) => {
    requireAdmin(locals.profile);
    try {
      const intent = await prepareFeishuIntent(
        '/integrations/feishu-base/connection/test/',
        FEISHU_BASE_ACTIONS.validateSchema,
        cookies,
        locals?.org
      );
      if (!intent) {
        return fail(502, { actionError: 'Feishu Base returned an invalid approval intent.' });
      }
      return { feishuApprovalRequired: true, feishuOperation: 'schema', feishuIntent: intent };
    } catch (cause) {
      return fixedFailure(cause, 'Could not prepare Feishu Base schema validation.');
    }
  },
  validateFeishuSchema: async ({ request, cookies, locals }) => {
    requireAdmin(locals.profile);
    const form = await request.formData();
    const approvalId = String(form.get('approval_id') || '').trim();
    try {
      if (
        !(await executeApprovedFeishuAction(
          '/integrations/feishu-base/connection/test/',
          approvalId,
          cookies,
          locals?.org
        ))
      ) {
        return fail(400, { actionError: 'Enter a valid one-time approval UUID.' });
      }
      return { feishuExecutionQueued: true, feishuOperation: 'schema' };
    } catch (cause) {
      return fixedFailure(cause, 'Could not queue Feishu Base schema validation.');
    }
  },
  prepareFeishuSync: async ({ request, cookies, locals }) => {
    requireAdmin(locals.profile);
    const form = await request.formData();
    const intakeId = String(form.get('intake_id') || '').trim();
    if (!UUID_PATTERN.test(intakeId)) return fail(400, { actionError: 'Select a valid intake.' });
    try {
      const intent = await prepareFeishuIntent(
        `/integrations/feishu-base/intakes/${intakeId}/sync/`,
        FEISHU_BASE_ACTIONS.syncResearch,
        cookies,
        locals?.org
      );
      if (!intent)
        return fail(502, { actionError: 'Feishu Base returned an invalid approval intent.' });
      return {
        feishuApprovalRequired: true,
        feishuOperation: 'sync',
        feishuObjectId: intakeId,
        feishuIntent: intent
      };
    } catch (cause) {
      return fixedFailure(cause, 'Could not prepare the Feishu Base research sync.');
    }
  },
  syncFeishuIntake: async ({ request, cookies, locals }) => {
    requireAdmin(locals.profile);
    const form = await request.formData();
    const intakeId = String(form.get('intake_id') || '').trim();
    const approvalId = String(form.get('approval_id') || '').trim();
    if (!UUID_PATTERN.test(intakeId)) return fail(400, { actionError: 'Select a valid intake.' });
    try {
      if (
        !(await executeApprovedFeishuAction(
          `/integrations/feishu-base/intakes/${intakeId}/sync/`,
          approvalId,
          cookies,
          locals?.org
        ))
      ) {
        return fail(400, { actionError: 'Enter a valid one-time approval UUID.' });
      }
      return { feishuExecutionQueued: true, feishuOperation: 'sync' };
    } catch (cause) {
      return fixedFailure(cause, 'Could not queue the Feishu Base research sync.');
    }
  },
  prepareFeishuDelete: async ({ request, cookies, locals }) => {
    requireAdmin(locals.profile);
    const form = await request.formData();
    const syncId = String(form.get('sync_id') || '').trim();
    if (!UUID_PATTERN.test(syncId))
      return fail(400, { actionError: 'Select a valid sync ledger item.' });
    try {
      const intent = await prepareFeishuIntent(
        `/integrations/feishu-base/syncs/${syncId}/delete/`,
        FEISHU_BASE_ACTIONS.deleteResearch,
        cookies,
        locals?.org
      );
      if (!intent)
        return fail(502, { actionError: 'Feishu Base returned an invalid approval intent.' });
      return {
        feishuApprovalRequired: true,
        feishuOperation: 'delete',
        feishuObjectId: syncId,
        feishuIntent: intent
      };
    } catch (cause) {
      return fixedFailure(cause, 'Could not prepare deletion of the Feishu Base research record.');
    }
  },
  deleteFeishuSync: async ({ request, cookies, locals }) => {
    requireAdmin(locals.profile);
    const form = await request.formData();
    const syncId = String(form.get('sync_id') || '').trim();
    const approvalId = String(form.get('approval_id') || '').trim();
    if (!UUID_PATTERN.test(syncId))
      return fail(400, { actionError: 'Select a valid sync ledger item.' });
    try {
      if (
        !(await executeApprovedFeishuAction(
          `/integrations/feishu-base/syncs/${syncId}/delete/`,
          approvalId,
          cookies,
          locals?.org
        ))
      ) {
        return fail(400, { actionError: 'Enter a valid one-time approval UUID.' });
      }
      return { feishuExecutionQueued: true, feishuOperation: 'delete' };
    } catch (cause) {
      return fixedFailure(cause, 'Could not queue deletion of the Feishu Base research record.');
    }
  }
};
