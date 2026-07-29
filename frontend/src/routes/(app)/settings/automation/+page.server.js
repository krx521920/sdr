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

/** @type {import('./$types').PageServerLoad} */
export async function load({ cookies, url }) {
  const requestedStatus = url.searchParams.get('status') || '';
  const status = VALID_STATUSES.has(requestedStatus) ? requestedStatus : '';
  const query = new URLSearchParams({ limit: '50' });
  if (status) query.set('status', status);

  try {
    const [jobs, intakes, responseSettings] = await Promise.all([
      apiRequest(`/automation/jobs/?${query}`, {}, { cookies }),
      apiRequest('/sdr/intakes/?limit=50', {}, { cookies }),
      apiRequest('/sdr/response-settings/', {}, { cookies })
    ]);
    return { jobs, intakes, responseSettings, selectedStatus: status, loadError: '' };
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
  }
};
