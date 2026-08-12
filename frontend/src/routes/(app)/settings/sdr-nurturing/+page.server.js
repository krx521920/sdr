import { apiRequest } from '$lib/api-helpers.js';
import { error, fail } from '@sveltejs/kit';

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

/** @param {FormData} formData @param {string} name */
function parseList(formData, name) {
  try {
    const parsed = JSON.parse(String(formData.get(name) || '[]'));
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

/** @param {FormData} formData */
function buildSequence(formData) {
  return {
    name: String(formData.get('name') || '').trim(),
    description: String(formData.get('description') || '').trim(),
    priority: Number(formData.get('priority') || 100),
    is_active: formData.has('is_active'),
    auto_enroll: formData.has('auto_enroll'),
    sources: parseList(formData, 'sources'),
    qualification_bands: parseList(formData, 'qualification_bands'),
    from_email: String(formData.get('from_email') || '').trim(),
    steps: parseList(formData, 'steps')
  };
}

/** @type {import('./$types').PageServerLoad} */
export async function load({ cookies, locals }) {
  const profile = locals.profile;
  if (profile?.role !== 'ADMIN' && !profile?.is_organization_admin) {
    throw error(403, 'Only admins can manage SDR nurturing');
  }
  try {
    const [nurture, enrollments, suppressions] = await Promise.all([
      apiRequest('/sdr/nurture/sequences/', {}, { cookies, org: locals?.org }),
      apiRequest('/sdr/nurture/enrollments/?limit=50', {}, { cookies, org: locals?.org }),
      apiRequest('/sdr/nurture/suppressions/?limit=100', {}, { cookies, org: locals?.org })
    ]);
    return { nurture, enrollments, suppressions };
  } catch (err) {
    console.error('Failed to load SDR nurture settings:', err);
    throw error(500, 'Failed to load SDR nurture settings');
  }
}

/** @type {import('./$types').Actions} */
export const actions = {
  create: async ({ request, cookies, locals }) => {
    try {
      const sequence = await apiRequest(
        '/sdr/nurture/sequences/',
        { method: 'POST', body: buildSequence(await request.formData()) },
        { cookies, org: locals?.org }
      );
      return { saved: true, sequenceId: sequence.id };
    } catch (err) {
      return fail(400, { actionError: err?.message || 'Could not create the nurture sequence.' });
    }
  },

  update: async ({ request, cookies, locals }) => {
    const formData = await request.formData();
    const id = String(formData.get('id') || '');
    if (!UUID_PATTERN.test(id)) return fail(400, { actionError: 'Invalid nurture sequence.' });
    try {
      await apiRequest(
        `/sdr/nurture/sequences/${id}/`,
        { method: 'PUT', body: buildSequence(formData) },
        { cookies, org: locals?.org }
      );
      return { saved: true, sequenceId: id };
    } catch (err) {
      return fail(400, { actionError: err?.message || 'Could not update the nurture sequence.' });
    }
  },

  delete: async ({ request, cookies, locals }) => {
    const id = String((await request.formData()).get('id') || '');
    if (!UUID_PATTERN.test(id)) return fail(400, { actionError: 'Invalid nurture sequence.' });
    try {
      await apiRequest(
        `/sdr/nurture/sequences/${id}/`,
        { method: 'DELETE' },
        { cookies, org: locals?.org }
      );
      return { deleted: true };
    } catch (err) {
      return fail(400, {
        actionError: err?.message || 'Disable sequences that already contain enrollment history.'
      });
    }
  },

  enrollmentAction: async ({ request, cookies, locals }) => {
    const formData = await request.formData();
    const id = String(formData.get('id') || '');
    const action = String(formData.get('enrollment_action') || '');
    if (!UUID_PATTERN.test(id)) return fail(400, { actionError: 'Invalid nurture enrollment.' });
    try {
      await apiRequest(
        `/sdr/nurture/enrollments/${id}/action/`,
        {
          method: 'POST',
          body: {
            action,
            reply_sentiment: String(formData.get('reply_sentiment') || 'neutral')
          }
        },
        { cookies, org: locals?.org }
      );
      return { enrollmentUpdated: true };
    } catch (err) {
      return fail(400, { actionError: err?.message || 'Could not update the enrollment.' });
    }
  },

  addSuppression: async ({ request, cookies, locals }) => {
    const formData = await request.formData();
    try {
      await apiRequest(
        '/sdr/nurture/suppressions/',
        {
          method: 'POST',
          body: {
            email: String(formData.get('email') || '').trim(),
            reason: String(formData.get('reason') || 'admin')
          }
        },
        { cookies, org: locals?.org }
      );
      return { suppressionAdded: true };
    } catch (err) {
      return fail(400, { actionError: err?.message || 'Could not suppress the email address.' });
    }
  },

  releaseSuppression: async ({ request, cookies, locals }) => {
    const id = String((await request.formData()).get('id') || '');
    if (!UUID_PATTERN.test(id)) return fail(400, { actionError: 'Invalid suppression record.' });
    try {
      await apiRequest(
        `/sdr/nurture/suppressions/${id}/`,
        { method: 'DELETE' },
        { cookies, org: locals?.org }
      );
      return { suppressionReleased: true };
    } catch (err) {
      return fail(400, { actionError: err?.message || 'Could not release the suppression.' });
    }
  }
};
