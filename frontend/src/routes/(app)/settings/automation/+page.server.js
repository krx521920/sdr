import { apiRequest } from '$lib/api-helpers.js';
import { fail } from '@sveltejs/kit';

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

/** @type {import('./$types').PageServerLoad} */
export async function load({ cookies, url }) {
  const requestedStatus = url.searchParams.get('status') || '';
  const status = VALID_STATUSES.has(requestedStatus) ? requestedStatus : '';
  const query = new URLSearchParams({ limit: '50' });
  if (status) query.set('status', status);

  try {
    const [jobs, intakes, responseSettings, feishuBase] = await Promise.all([
      apiRequest(`/automation/jobs/?${query}`, {}, { cookies }),
      apiRequest('/sdr/intakes/?limit=50', {}, { cookies }),
      apiRequest('/sdr/response-settings/', {}, { cookies }),
      apiRequest('/integrations/feishu-base/connection/', {}, { cookies })
    ]);
    return { jobs, intakes, responseSettings, feishuBase, selectedStatus: status, loadError: '' };
  } catch (error) {
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
      feishuBase: {},
      selectedStatus: status,
      loadError: error?.message || 'Could not load automation jobs.'
    };
  }
}

/** @type {import('./$types').Actions} */
export const actions = {
  retry: async ({ request, cookies }) => {
    const formData = await request.formData();
    const jobId = formData.get('job_id')?.toString() || '';
    if (!UUID_PATTERN.test(jobId)) {
      return fail(400, { actionError: 'The automation job is invalid.' });
    }
    try {
      await apiRequest(`/automation/jobs/${jobId}/retry/`, { method: 'POST' }, { cookies });
      return { retried: true };
    } catch (error) {
      return fail(400, {
        actionError: error?.message || 'Could not retry the automation job.'
      });
    }
  },
  saveResponse: async ({ request, cookies }) => {
    const formData = await request.formData();
    const body = {
      acknowledgement_email_enabled: formData.has('acknowledgement_email_enabled'),
      acknowledgement_subject: String(formData.get('acknowledgement_subject') || '').trim(),
      acknowledgement_body: String(formData.get('acknowledgement_body') || '').trim(),
      acknowledgement_from_email: String(
        formData.get('acknowledgement_from_email') || ''
      ).trim(),
      sales_in_app_enabled: formData.has('sales_in_app_enabled'),
      feishu_enabled: formData.has('feishu_enabled'),
      feishu_webhook_url: String(formData.get('feishu_webhook_url') || '').trim(),
      clear_feishu_webhook: formData.has('clear_feishu_webhook'),
      response_sla_seconds: Number(formData.get('response_sla_seconds') || 60)
    };
    try {
      await apiRequest(
        '/sdr/response-settings/',
        { method: 'PUT', body },
        { cookies }
      );
      return { responseSaved: true };
    } catch (error) {
      return fail(400, {
        actionError: error?.message || 'Could not save lead response settings.'
      });
    }
  },
  saveFeishuBase: async ({ request, cookies }) => {
    const formData = await request.formData();
    const fieldMapping = {};
    for (const key of FEISHU_BASE_MAPPING_KEYS) {
      const fieldName = String(formData.get(`mapping_${key}`) || '').trim();
      if (fieldName) fieldMapping[key] = fieldName;
    }
    const body = {
      app_id: String(formData.get('app_id') || '').trim(),
      app_secret: String(formData.get('app_secret') || '').trim() || undefined,
      app_token: String(formData.get('app_token') || '').trim(),
      table_id: String(formData.get('table_id') || '').trim(),
      field_mapping: fieldMapping,
      is_active: formData.has('is_active')
    };
    try {
      await apiRequest(
        '/integrations/feishu-base/connection/',
        { method: 'PUT', body },
        { cookies }
      );
      return { feishuBaseSaved: true };
    } catch (error) {
      return fail(400, {
        actionError: error?.message || 'Could not save the Feishu Base connection.'
      });
    }
  },
  testFeishuBase: async ({ cookies }) => {
    try {
      const result = await apiRequest(
        '/integrations/feishu-base/connection/test/',
        { method: 'POST' },
        { cookies }
      );
      return { feishuBaseTested: true, feishuBaseFieldCount: result.field_count || 0 };
    } catch (error) {
      return fail(400, {
        actionError: error?.message || 'Could not validate the Feishu Base connection.'
      });
    }
  }
};
