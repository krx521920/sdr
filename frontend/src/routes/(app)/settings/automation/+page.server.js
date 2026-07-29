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
    const jobs = await apiRequest(`/automation/jobs/?${query}`, {}, { cookies });
    return { jobs, selectedStatus: status, loadError: '' };
  } catch (error) {
    return {
      jobs: { count: 0, summary: {}, results: [] },
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
  }
};
