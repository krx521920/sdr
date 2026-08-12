import { apiRequest } from '$lib/api-helpers.js';
import { error, fail } from '@sveltejs/kit';

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

/** @type {import('./$types').PageServerLoad} */
export async function load({ cookies, locals, url }) {
  const profile = locals.profile;
  if (profile?.role !== 'ADMIN' && !profile?.is_organization_admin) {
    throw error(403, 'Only admins can manage outbound prospecting');
  }

  try {
    const campaigns = await apiRequest(
      '/sdr/outbound/campaigns/',
      {},
      { cookies, org: locals?.org }
    );
    const requestedId = url.searchParams.get('campaign') || '';
    const selectedCampaign =
      campaigns.results?.find((item) => item.id === requestedId) || campaigns.results?.[0] || null;
    const prospects = selectedCampaign
      ? await apiRequest(
          `/sdr/outbound/campaigns/${selectedCampaign.id}/prospects/?limit=250`,
          {},
          { cookies, org: locals?.org }
        )
      : { count: 0, summary: {}, results: [] };
    return { campaigns, prospects, selectedCampaign, loadError: '' };
  } catch (err) {
    console.error('Failed to load SDR outbound:', err);
    return {
      campaigns: { summary: {}, channels: [], statuses: [], results: [] },
      prospects: { count: 0, summary: {}, results: [] },
      selectedCampaign: null,
      loadError: err?.message || 'Could not load outbound prospecting.'
    };
  }
}

/** @type {import('./$types').Actions} */
export const actions = {
  saveCampaign: async ({ request, cookies, locals }) => {
    const formData = await request.formData();
    const campaignId = String(formData.get('campaign_id') || '');
    if (campaignId && !UUID_PATTERN.test(campaignId)) {
      return fail(400, { actionError: 'The outbound campaign is invalid.' });
    }
    const body = {
      name: String(formData.get('name') || '').trim(),
      description: String(formData.get('description') || '').trim(),
      icp_description: String(formData.get('icp_description') || '').trim(),
      channels: formData.getAll('channels').map(String),
      status: String(formData.get('status') || 'draft'),
      sequence_id: String(formData.get('sequence_id') || '') || null,
      daily_send_limit: Number(formData.get('daily_send_limit') || 50)
    };
    if (!body.name) return fail(400, { actionError: 'Campaign name is required.' });
    try {
      const saved = await apiRequest(
        campaignId
          ? `/sdr/outbound/campaigns/${campaignId}/`
          : '/sdr/outbound/campaigns/',
        { method: campaignId ? 'PATCH' : 'POST', body },
        { cookies, org: locals?.org }
      );
      return { campaignSaved: true, campaignId: saved.id };
    } catch (err) {
      return fail(400, {
        actionError: err?.message || 'Could not save the outbound campaign.'
      });
    }
  },
  campaignAction: async ({ request, cookies, locals }) => {
    const formData = await request.formData();
    const campaignId = String(formData.get('campaign_id') || '');
    const action = String(formData.get('campaign_action') || '');
    const allowed = ['launch', 'pause', 'retry_failed', 'complete', 'archive'];
    if (!UUID_PATTERN.test(campaignId) || !allowed.includes(action)) {
      return fail(400, { actionError: 'The campaign action is invalid.' });
    }
    try {
      const result = await apiRequest(
        `/sdr/outbound/campaigns/${campaignId}/action/`,
        { method: 'POST', body: { action } },
        { cookies, org: locals?.org }
      );
      return { campaignUpdated: true, campaignAction: action, campaignExecution: result.execution };
    } catch (err) {
      return fail(400, {
        actionError: err?.message || 'Could not update campaign execution.'
      });
    }
  },
  importProspects: async ({ request, cookies, locals }) => {
    const formData = await request.formData();
    const campaignId = String(formData.get('campaign_id') || '');
    const csvText = String(formData.get('csv_text') || '');
    if (!UUID_PATTERN.test(campaignId)) {
      return fail(400, { actionError: 'Select an outbound campaign first.' });
    }
    if (!csvText.trim()) return fail(400, { actionError: 'Paste a CSV prospect list.' });
    try {
      const importResult = await apiRequest(
        `/sdr/outbound/campaigns/${campaignId}/prospects/import/`,
        {
          method: 'POST',
          body: { csv_text: csvText, promote_ready: formData.has('promote_ready') }
        },
        { cookies, org: locals?.org }
      );
      return { prospectsImported: true, importResult };
    } catch (err) {
      return fail(400, {
        actionError: err?.message || 'Could not import the prospect list.'
      });
    }
  },
  prospectAction: async ({ request, cookies, locals }) => {
    const formData = await request.formData();
    const prospectId = String(formData.get('prospect_id') || '');
    const action = String(formData.get('prospect_action') || '');
    if (!UUID_PATTERN.test(prospectId) || !['promote', 'disqualify', 'restore'].includes(action)) {
      return fail(400, { actionError: 'The prospect action is invalid.' });
    }
    try {
      await apiRequest(
        `/sdr/outbound/prospects/${prospectId}/action/`,
        { method: 'POST', body: { action } },
        { cookies, org: locals?.org }
      );
      return { prospectUpdated: true, prospectAction: action };
    } catch (err) {
      return fail(400, {
        actionError: err?.message || 'Could not update the prospect.'
      });
    }
  }
};
