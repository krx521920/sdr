import { apiRequest } from '$lib/api-helpers.js';
import { error, fail } from '@sveltejs/kit';

/** @type {import('./$types').PageServerLoad} */
export async function load({ cookies, locals }) {
  const profile = locals.profile;
  if (profile?.role !== 'ADMIN' && !profile?.is_organization_admin) {
    throw error(403, 'Only admins can manage SDR routing rules');
  }

  try {
    const [routing, users] = await Promise.all([
      apiRequest('/sdr/routing-rules/', {}, { cookies, org: locals?.org }),
      apiRequest('/users/', {}, { cookies, org: locals?.org }).catch(() => ({}))
    ]);
    const activeUsers = users.active_users?.active_users || [];
    return {
      ...routing,
      profiles: activeUsers
        .filter((user) => user.is_active && user.has_sales_access)
        .map((user) => ({
          id: user.id,
          name: user.user_details?.name || user.user_details?.email || 'Sales user',
          email: user.user_details?.email || ''
        }))
    };
  } catch (err) {
    console.error('Failed to load SDR routing rules:', err);
    throw error(500, 'Failed to load SDR routing rules');
  }
}

/** @param {FormData} formData */
function buildBody(formData) {
  const parseList = (name) => {
    try {
      const value = JSON.parse(String(formData.get(name) || '[]'));
      return Array.isArray(value) ? value : [];
    } catch {
      return [];
    }
  };
  return {
    name: String(formData.get('name') || '').trim(),
    priority: Number(formData.get('priority') || 100),
    strategy: String(formData.get('strategy') || 'least_loaded'),
    is_active: formData.has('is_active'),
    countries: String(formData.get('countries') || '')
      .split(',')
      .map((value) => value.trim())
      .filter(Boolean),
    sources: parseList('sources'),
    qualification_bands: parseList('qualification_bands'),
    profile_ids: parseList('profile_ids')
  };
}

/** @type {import('./$types').Actions} */
export const actions = {
  create: async ({ request, cookies, locals }) => {
    const body = buildBody(await request.formData());
    try {
      const created = await apiRequest(
        '/sdr/routing-rules/',
        { method: 'POST', body },
        { cookies, org: locals?.org }
      );
      return { saved: true, ruleId: created.id };
    } catch (err) {
      return fail(400, { actionError: err?.message || 'Could not create routing rule.' });
    }
  },

  update: async ({ request, cookies, locals }) => {
    const formData = await request.formData();
    const id = String(formData.get('id') || '');
    if (!id) return fail(400, { actionError: 'Missing routing rule.' });
    try {
      await apiRequest(
        `/sdr/routing-rules/${id}/`,
        { method: 'PUT', body: buildBody(formData) },
        { cookies, org: locals?.org }
      );
      return { saved: true, ruleId: id };
    } catch (err) {
      return fail(400, { actionError: err?.message || 'Could not update routing rule.' });
    }
  },

  delete: async ({ request, cookies, locals }) => {
    const id = String((await request.formData()).get('id') || '');
    if (!id) return fail(400, { actionError: 'Missing routing rule.' });
    try {
      await apiRequest(
        `/sdr/routing-rules/${id}/`,
        { method: 'DELETE' },
        { cookies, org: locals?.org }
      );
      return { deleted: true };
    } catch (err) {
      return fail(400, { actionError: err?.message || 'Could not delete routing rule.' });
    }
  },

  preview: async ({ request, cookies, locals }) => {
    const formData = await request.formData();
    try {
      const preview = await apiRequest(
        '/sdr/routing-rules/preview/',
        {
          method: 'POST',
          body: {
            country: String(formData.get('country') || '').trim(),
            source: String(formData.get('source') || 'api'),
            qualification_band: String(formData.get('qualification_band') || 'medium')
          }
        },
        { cookies, org: locals?.org }
      );
      return { preview };
    } catch (err) {
      return fail(400, { actionError: err?.message || 'Could not preview routing.' });
    }
  }
};
