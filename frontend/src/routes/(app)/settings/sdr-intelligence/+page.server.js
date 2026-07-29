import { apiRequest } from '$lib/api-helpers.js';
import { error, fail } from '@sveltejs/kit';

/** @type {import('./$types').PageServerLoad} */
export async function load({ cookies, locals }) {
  const profile = locals.profile;
  if (profile?.role !== 'ADMIN' && !profile?.is_organization_admin) {
    throw error(403, 'Only admins can manage the AI lead inspector');
  }
  try {
    const [configuration, inspections] = await Promise.all([
      apiRequest('/sdr/intelligence/settings/', {}, { cookies, org: locals?.org }),
      apiRequest('/sdr/intelligence/inspections/?limit=50', {}, { cookies, org: locals?.org })
    ]);
    return { configuration, inspections };
  } catch (err) {
    console.error('Failed to load SDR intelligence:', err);
    throw error(500, 'Failed to load the AI lead inspector');
  }
}

/** @type {import('./$types').Actions} */
export const actions = {
  save: async ({ request, cookies, locals }) => {
    const formData = await request.formData();
    const body = {
      is_enabled: formData.has('is_enabled'),
      research_enabled: formData.has('research_enabled'),
      ai_scoring_enabled: formData.has('ai_scoring_enabled'),
      provider: String(formData.get('provider') || 'openai'),
      model: String(formData.get('model') || '').trim(),
      reasoning_effort: String(formData.get('reasoning_effort') || 'low'),
      fallback_provider: String(formData.get('fallback_provider') || ''),
      fallback_model: String(formData.get('fallback_model') || '').trim(),
      fallback_reasoning_effort: String(formData.get('fallback_reasoning_effort') || 'low'),
      icp_description: String(formData.get('icp_description') || '').trim(),
      positive_signals: String(formData.get('positive_signals') || '').trim(),
      negative_signals: String(formData.get('negative_signals') || '').trim(),
      max_research_pages: Number(formData.get('max_research_pages') || 2),
      website_timeout_seconds: Number(formData.get('website_timeout_seconds') || 5),
      openai_api_key: String(formData.get('openai_api_key') || '').trim(),
      doubao_api_key: String(formData.get('doubao_api_key') || '').trim(),
      deepseek_api_key: String(formData.get('deepseek_api_key') || '').trim(),
      clear_openai_api_key: formData.has('clear_openai_api_key'),
      clear_doubao_api_key: formData.has('clear_doubao_api_key'),
      clear_deepseek_api_key: formData.has('clear_deepseek_api_key')
    };
    try {
      await apiRequest(
        '/sdr/intelligence/settings/',
        { method: 'PUT', body },
        { cookies, org: locals?.org }
      );
      return { saved: true };
    } catch (err) {
      return fail(400, {
        actionError: err?.message || 'Could not save AI lead inspector settings.'
      });
    }
  }
};
