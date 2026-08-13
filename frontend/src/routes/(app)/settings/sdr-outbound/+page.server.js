import { apiRequest } from '$lib/api-helpers.js';
import { error, fail } from '@sveltejs/kit';

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

function csvValues(value) {
  return [
    ...new Set(
      String(value || '')
        .split(',')
        .map((item) => item.trim())
        .filter(Boolean)
    )
  ];
}

/** @type {import('./$types').PageServerLoad} */
export async function load({ cookies, locals, url }) {
  const profile = locals.profile;
  if (profile?.role !== 'ADMIN' && !profile?.is_organization_admin) {
    throw error(403, 'Only admins can manage outbound prospecting');
  }

  let whatsappConnection = null;
  let whatsappError = '';
  let apolloConnection = null;
  let apolloError = '';
  let linkedinConnection = null;
  let linkedinError = '';
  try {
    whatsappConnection = await apiRequest(
      '/integrations/whatsapp/connection/',
      {},
      { cookies, org: locals?.org }
    );
  } catch (err) {
    if (!err?.message?.startsWith('HTTP 404')) {
      whatsappError = err?.message || 'Could not load WhatsApp settings.';
    }
  }

  try {
    apolloConnection = await apiRequest(
      '/integrations/apollo/connection/',
      {},
      { cookies, org: locals?.org }
    );
  } catch (err) {
    if (!err?.message?.startsWith('HTTP 404')) {
      apolloError = err?.message || 'Could not load Apollo settings.';
    }
  }

  try {
    linkedinConnection = await apiRequest(
      '/integrations/linkedin/connection/',
      {},
      { cookies, org: locals?.org }
    );
  } catch (err) {
    if (!err?.message?.startsWith('HTTP 404')) {
      linkedinError = err?.message || 'Could not load LinkedIn settings.';
    }
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
    const [prospects, outboundSources, copyDrafts, campaignAnalytics] = selectedCampaign
      ? await Promise.all([
          apiRequest(
            `/sdr/outbound/campaigns/${selectedCampaign.id}/prospects/?limit=250`,
            {},
            { cookies, org: locals?.org }
          ),
          apiRequest(
            `/sdr/outbound/campaigns/${selectedCampaign.id}/sources/`,
            {},
            { cookies, org: locals?.org }
          ),
          apiRequest(
            `/sdr/outbound/campaigns/${selectedCampaign.id}/copy-drafts/`,
            {},
            { cookies, org: locals?.org }
          ),
          apiRequest(
            `/sdr/outbound/campaigns/${selectedCampaign.id}/analytics/`,
            {},
            { cookies, org: locals?.org }
          )
        ])
      : [{ count: 0, summary: {}, results: [] }, [], [], null];
    return {
      campaigns,
      prospects,
      selectedCampaign,
      whatsappConnection,
      whatsappError,
      apolloConnection,
      apolloError,
      linkedinConnection,
      linkedinError,
      outboundSources,
      copyDrafts,
      campaignAnalytics,
      loadError: ''
    };
  } catch (err) {
    console.error('Failed to load SDR outbound:', err);
    return {
      campaigns: { summary: {}, channels: [], statuses: [], results: [] },
      prospects: { count: 0, summary: {}, results: [] },
      selectedCampaign: null,
      whatsappConnection,
      whatsappError,
      apolloConnection,
      apolloError,
      linkedinConnection,
      linkedinError,
      outboundSources: [],
      copyDrafts: [],
      campaignAnalytics: null,
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
      daily_send_limit: Number(formData.get('daily_send_limit') || 50),
      linkedin_invitation_message: String(
        formData.get('linkedin_invitation_message') || ''
      ).trim(),
      whatsapp_template_name: String(formData.get('whatsapp_template_name') || '').trim(),
      whatsapp_template_language: String(
        formData.get('whatsapp_template_language') || 'en_US'
      ).trim()
    };
    if (!body.name) return fail(400, { actionError: 'Campaign name is required.' });
    try {
      const saved = await apiRequest(
        campaignId ? `/sdr/outbound/campaigns/${campaignId}/` : '/sdr/outbound/campaigns/',
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
  saveWhatsAppConnection: async ({ request, cookies, locals }) => {
    const formData = await request.formData();
    const body = {
      phone_number_id: String(formData.get('phone_number_id') || '').trim(),
      business_account_id: String(formData.get('business_account_id') || '').trim(),
      display_phone_number: String(formData.get('display_phone_number') || '').trim(),
      is_active: formData.has('is_active')
    };
    const accessToken = String(formData.get('access_token') || '').trim();
    if (accessToken) body.access_token = accessToken;
    if (!/^\d{1,64}$/.test(body.phone_number_id)) {
      return fail(400, { actionError: 'Enter the numeric WhatsApp Phone Number ID.' });
    }
    try {
      const connection = await apiRequest(
        '/integrations/whatsapp/connection/',
        { method: 'PUT', body },
        { cookies, org: locals?.org }
      );
      return { whatsappSaved: true, whatsappConnection: connection };
    } catch (err) {
      return fail(400, {
        actionError: err?.message || 'Could not save the WhatsApp connection.'
      });
    }
  },
  saveLinkedInConnection: async ({ request, cookies, locals }) => {
    const formData = await request.formData();
    const body = {
      is_active: formData.has('is_active'),
      partner_access_confirmed: formData.has('partner_access_confirmed')
    };
    const accessToken = String(formData.get('access_token') || '').trim();
    if (accessToken) body.access_token = accessToken;
    try {
      const connection = await apiRequest(
        '/integrations/linkedin/connection/',
        { method: 'PUT', body },
        { cookies, org: locals?.org }
      );
      return { linkedinSaved: true, linkedinConnection: connection };
    } catch (err) {
      return fail(400, {
        actionError: err?.message || 'Could not save the LinkedIn connection.'
      });
    }
  },
  saveApolloConnection: async ({ request, cookies, locals }) => {
    const formData = await request.formData();
    const body = { is_active: formData.has('is_active') };
    const apiKey = String(formData.get('api_key') || '').trim();
    if (apiKey) body.api_key = apiKey;
    try {
      const connection = await apiRequest(
        '/integrations/apollo/connection/',
        { method: 'PUT', body },
        { cookies, org: locals?.org }
      );
      return { apolloSaved: true, apolloConnection: connection };
    } catch (err) {
      return fail(400, {
        actionError: err?.message || 'Could not save the Apollo connection.'
      });
    }
  },
  saveApolloSource: async ({ request, cookies, locals }) => {
    const formData = await request.formData();
    const campaignId = String(formData.get('campaign_id') || '');
    if (!UUID_PATTERN.test(campaignId)) {
      return fail(400, { actionError: 'Select an outbound campaign first.' });
    }
    const searchFilters = {};
    for (const [field, formName] of [
      ['person_titles', 'person_titles'],
      ['person_seniorities', 'person_seniorities'],
      ['person_locations', 'person_locations'],
      ['organization_locations', 'organization_locations'],
      ['organization_domains', 'organization_domains'],
      ['employee_ranges', 'employee_ranges']
    ]) {
      const values = csvValues(formData.get(formName));
      if (values.length) searchFilters[field] = values;
    }
    const keywords = String(formData.get('keywords') || '').trim();
    if (keywords) searchFilters.keywords = keywords;
    const body = {
      name: String(formData.get('name') || '').trim(),
      is_active: formData.has('is_active'),
      search_filters: searchFilters,
      interval_hours: Number(formData.get('interval_hours') || 24),
      max_results_per_sync: Number(formData.get('max_results_per_sync') || 25),
      enrichment_credits_acknowledged: formData.has('enrichment_credits_acknowledged')
    };
    if (!body.name) return fail(400, { actionError: 'Apollo source name is required.' });
    try {
      const source = await apiRequest(
        `/sdr/outbound/campaigns/${campaignId}/sources/`,
        { method: 'POST', body },
        { cookies, org: locals?.org }
      );
      return { apolloSourceSaved: true, apolloSource: source };
    } catch (err) {
      return fail(400, {
        actionError: err?.message || 'Could not save the Apollo source.'
      });
    }
  },
  syncApolloSource: async ({ request, cookies, locals }) => {
    const formData = await request.formData();
    const sourceId = String(formData.get('source_id') || '');
    if (!UUID_PATTERN.test(sourceId)) {
      return fail(400, { actionError: 'The Apollo source is invalid.' });
    }
    try {
      const result = await apiRequest(
        `/sdr/outbound/sources/${sourceId}/sync/`,
        { method: 'POST', body: {} },
        { cookies, org: locals?.org }
      );
      return { apolloSourceQueued: true, apolloSourceJob: result };
    } catch (err) {
      return fail(400, {
        actionError: err?.message || 'Could not queue the Apollo source sync.'
      });
    }
  },
  generateOutboundCopy: async ({ request, cookies, locals }) => {
    const formData = await request.formData();
    const campaignId = String(formData.get('campaign_id') || '');
    if (!UUID_PATTERN.test(campaignId)) {
      return fail(400, { actionError: 'Select an outbound campaign first.' });
    }
    const body = {
      language: String(formData.get('language') || 'English').trim(),
      tone: String(formData.get('tone') || 'concise and consultative').trim(),
      offering_summary: String(formData.get('offering_summary') || '').trim(),
      value_proposition: String(formData.get('value_proposition') || '').trim(),
      proof_points: String(formData.get('proof_points') || '').trim(),
      cta_goal: String(formData.get('cta_goal') || '').trim(),
      step_count: Number(formData.get('step_count') || 3)
    };
    if (!body.offering_summary || !body.value_proposition || !body.cta_goal) {
      return fail(400, { actionError: 'Offering, value proposition, and CTA goal are required.' });
    }
    try {
      const draft = await apiRequest(
        `/sdr/outbound/campaigns/${campaignId}/copy-drafts/`,
        { method: 'POST', body },
        { cookies, org: locals?.org }
      );
      return { outboundCopyQueued: true, outboundCopyDraft: draft };
    } catch (err) {
      return fail(400, {
        actionError: err?.message || 'Could not queue outbound copy generation.'
      });
    }
  },
  saveOutboundCopy: async ({ request, cookies, locals }) => {
    const formData = await request.formData();
    const draftId = String(formData.get('draft_id') || '');
    const stepCount = Number(formData.get('step_count') || 0);
    if (!UUID_PATTERN.test(draftId) || stepCount < 1 || stepCount > 5) {
      return fail(400, { actionError: 'The outbound copy draft is invalid.' });
    }
    const generatedSteps = [];
    for (let position = 1; position <= stepCount; position += 1) {
      generatedSteps.push({
        position,
        delay_days: Number(formData.get(`delay_days_${position}`) || 0),
        subject_a: String(formData.get(`subject_a_${position}`) || '').trim(),
        opening_a: String(formData.get(`opening_a_${position}`) || '').trim(),
        body_a: String(formData.get(`body_a_${position}`) || '').trim(),
        cta_a: String(formData.get(`cta_a_${position}`) || '').trim(),
        subject_b: String(formData.get(`subject_b_${position}`) || '').trim(),
        opening_b: String(formData.get(`opening_b_${position}`) || '').trim(),
        body_b: String(formData.get(`body_b_${position}`) || '').trim(),
        cta_b: String(formData.get(`cta_b_${position}`) || '').trim(),
        rationale: String(formData.get(`rationale_${position}`) || '').trim()
      });
    }
    try {
      const draft = await apiRequest(
        `/sdr/outbound/copy-drafts/${draftId}/`,
        { method: 'PATCH', body: { generated_steps: generatedSteps } },
        { cookies, org: locals?.org }
      );
      return { outboundCopySaved: true, outboundCopyDraft: draft };
    } catch (err) {
      return fail(400, {
        actionError: err?.message || 'Could not save the reviewed outbound copy.'
      });
    }
  },
  applyOutboundCopy: async ({ request, cookies, locals }) => {
    const formData = await request.formData();
    const draftId = String(formData.get('draft_id') || '');
    if (!UUID_PATTERN.test(draftId)) {
      return fail(400, { actionError: 'The outbound copy draft is invalid.' });
    }
    try {
      const result = await apiRequest(
        `/sdr/outbound/copy-drafts/${draftId}/action/`,
        { method: 'POST', body: { action: 'apply' } },
        { cookies, org: locals?.org }
      );
      return { outboundCopyApplied: true, outboundCopyApplyResult: result };
    } catch (err) {
      return fail(400, {
        actionError: err?.message || 'Could not apply the outbound copy.'
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
