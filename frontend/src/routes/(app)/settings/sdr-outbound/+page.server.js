import { apiRequest } from '$lib/api-helpers.js';
import {
  buildApolloApprovedExecution,
  normalizeApolloApprovalRequired,
  normalizeApolloCandidateResponse,
  validApolloUuid
} from '$lib/apollo-execution.js';
import { logSafeServerError } from '$lib/server/safe-error-log.js';
import {
  buildWhatsAppApprovedExecution,
  normalizeWhatsAppApprovalRequired,
  normalizeWhatsAppConnection,
  normalizeWhatsAppExecutionResult,
  normalizeWhatsAppMessageResponse,
  validWhatsAppUuid
} from '$lib/whatsapp-execution.js';
import { env } from '$env/dynamic/public';
import { error, fail } from '@sveltejs/kit';

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const LOCAL_CONNECTION_TEST_PROVIDERS = new Set(['apollo', 'whatsapp', 'linkedin']);
const LOCAL_CONNECTION_TEST_CODES = new Set([
  'connection_ready',
  'connection_missing',
  'connection_inactive',
  'required_identifier_missing',
  'credential_missing',
  'credential_decryption_failed',
  'partner_access_not_confirmed',
  'permission_denied'
]);
const APOLLO_CANDIDATE_SOURCE_LIMIT = 12;
const APOLLO_CANDIDATE_PER_SOURCE_LIMIT = 100;

function fixedOutboundFailure(err, fallback) {
  logSafeServerError('SDR outbound operation failed', err);
  const status = Number(err?.status);
  return fail([400, 403, 409].includes(status) ? status : 400, { actionError: fallback });
}

async function loadApolloCandidates(sources, cookies, org) {
  const safeSources = (Array.isArray(sources) ? sources : [])
    .filter((source) => source?.provider === 'apollo' && validApolloUuid(source?.id))
    .slice(0, APOLLO_CANDIDATE_SOURCE_LIMIT);
  const entries = await Promise.all(
    safeSources.map(async (source) => {
      try {
        const response = await apiRequest(
          `/sdr/outbound/sources/${source.id}/apollo-candidates/`,
          {},
          { cookies, org }
        );
        return [
          source.id,
          {
            ...normalizeApolloCandidateResponse(
              response,
              source.id,
              APOLLO_CANDIDATE_PER_SOURCE_LIMIT
            ),
            error: ''
          }
        ];
      } catch (err) {
        logSafeServerError('Could not load Apollo candidates', err);
        return [
          source.id,
          {
            count: 0,
            results: [],
            error: 'Apollo candidates could not be loaded for this source.'
          }
        ];
      }
    })
  );
  return Object.fromEntries(entries);
}

async function loadWhatsAppMessages(campaignId, cookies, org) {
  if (!validWhatsAppUuid(campaignId)) return { count: 0, results: [], error: '' };
  try {
    const response = await apiRequest(
      `/integrations/whatsapp/messages/?campaign_id=${encodeURIComponent(campaignId)}&limit=100`,
      {},
      { cookies, org }
    );
    return {
      ...normalizeWhatsAppMessageResponse(response, campaignId, 100),
      error: ''
    };
  } catch (err) {
    logSafeServerError('Could not load the WhatsApp execution queue', err);
    return {
      count: 0,
      results: [],
      error: 'The WhatsApp execution queue could not be loaded.'
    };
  }
}

/**
 * Check only the locally stored connection state. The backend endpoint is
 * deliberately read-only and returns no provider data, credential, or hint.
 * Keep the action response equally small even if the upstream contract changes.
 *
 * @param {'apollo' | 'whatsapp' | 'linkedin'} provider
 * @param {import('@sveltejs/kit').Cookies} cookies
 */
async function checkLocalConnection(provider, cookies) {
  if (!LOCAL_CONNECTION_TEST_PROVIDERS.has(provider)) {
    throw new Error('Unsupported local connection check.');
  }

  const accessToken = cookies.get('jwt_access');
  const response = await fetch(
    `${env.PUBLIC_DJANGO_API_URL}/api/integrations/${provider}/connection/test/`,
    {
      method: 'POST',
      headers: {
        Accept: 'application/json',
        'Content-Type': 'application/json',
        ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {})
      },
      body: '{}'
    }
  );
  const payload = await response.json().catch(() => null);
  const code = typeof payload?.code === 'string' ? payload.code : '';

  if (
    !LOCAL_CONNECTION_TEST_CODES.has(code) ||
    typeof payload?.ok !== 'boolean' ||
    payload?.local_only !== true
  ) {
    throw new Error('Unexpected local connection check response.');
  }

  return {
    code,
    ok: payload.ok,
    localOnly: true
  };
}

/**
 * @param {'apollo' | 'whatsapp' | 'linkedin'} provider
 * @param {import('@sveltejs/kit').Cookies} cookies
 */
async function localConnectionTestAction(provider, cookies) {
  try {
    return await checkLocalConnection(provider, cookies);
  } catch (err) {
    logSafeServerError('Local provider configuration check failed', err);
    return null;
  }
}

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
    whatsappConnection = normalizeWhatsAppConnection(
      await apiRequest('/integrations/whatsapp/connection/', {}, { cookies, org: locals?.org })
    );
  } catch (err) {
    if (Number(err?.status) !== 404) {
      logSafeServerError('Could not load WhatsApp settings', err);
      whatsappError = 'Could not load WhatsApp settings.';
    }
  }

  try {
    apolloConnection = await apiRequest(
      '/integrations/apollo/connection/',
      {},
      { cookies, org: locals?.org }
    );
  } catch (err) {
    if (Number(err?.status) !== 404) {
      logSafeServerError('Could not load Apollo settings', err);
      apolloError = 'Could not load Apollo settings.';
    }
  }

  try {
    linkedinConnection = await apiRequest(
      '/integrations/linkedin/connection/',
      {},
      { cookies, org: locals?.org }
    );
  } catch (err) {
    if (Number(err?.status) !== 404) {
      logSafeServerError('Could not load LinkedIn settings', err);
      linkedinError = 'Could not load LinkedIn settings.';
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
    const [prospects, outboundSources, copyDrafts, campaignAnalytics, whatsappMessages] =
      selectedCampaign
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
            ),
            loadWhatsAppMessages(selectedCampaign.id, cookies, locals?.org)
          ])
        : [
            { count: 0, summary: {}, results: [] },
            [],
            [],
            null,
            { count: 0, results: [], error: '' }
          ];
    const apolloCandidates = await loadApolloCandidates(outboundSources, cookies, locals?.org);
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
      apolloCandidates,
      copyDrafts,
      campaignAnalytics,
      whatsappMessages,
      loadError: ''
    };
  } catch (err) {
    logSafeServerError('Could not load SDR outbound', err);
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
      apolloCandidates: {},
      copyDrafts: [],
      campaignAnalytics: null,
      whatsappMessages: { count: 0, results: [], error: '' },
      loadError: 'Could not load outbound prospecting.'
    };
  }
}

/** @type {import('./$types').Actions} */
export const actions = {
  executeWhatsAppMessage: async ({ request, cookies, locals }) => {
    if (locals.profile?.role !== 'ADMIN' && !locals.profile?.is_organization_admin) {
      return fail(403, { actionError: 'Administrator access is required.' });
    }
    const formData = await request.formData();
    const messageId = String(formData.get('message_id') || '').trim();
    const approvalValue = String(formData.get('approval_id') || '').trim();
    if (!validWhatsAppUuid(messageId)) {
      return fail(400, { actionError: 'The WhatsApp message is invalid.' });
    }
    const body = approvalValue ? buildWhatsAppApprovedExecution(approvalValue) : {};
    if (!body) {
      return fail(400, { actionError: 'Enter a valid one-time approval UUID.' });
    }

    try {
      const response = await apiRequest(
        `/integrations/whatsapp/messages/${messageId}/execution/`,
        { method: 'POST', body },
        { cookies, org: locals?.org }
      );
      if (!approvalValue) {
        const intent = normalizeWhatsAppApprovalRequired(response, messageId);
        if (!intent) {
          return fail(502, {
            actionError: 'WhatsApp returned an unexpected approval-intent response.'
          });
        }
        return {
          whatsappApprovalRequired: true,
          whatsappMessageId: messageId,
          whatsappIntent: intent
        };
      }

      const execution = normalizeWhatsAppExecutionResult(response);
      if (!execution) {
        return fail(502, {
          actionError: 'WhatsApp returned an unexpected execution acknowledgement.'
        });
      }
      return {
        whatsappExecutionQueued: execution.executionStatus === 'reserved',
        whatsappExecutionConverged: ['accepted', 'delivered'].includes(execution.executionStatus),
        whatsappMessageId: messageId,
        whatsappExecution: execution
      };
    } catch (err) {
      logSafeServerError('WhatsApp execution request failed', err);
      if (Number(err?.status) === 403) {
        return fail(403, { actionError: 'Your administrator permission changed.' });
      }
      if (Number(err?.status) === 409) {
        return fail(409, {
          actionError:
            'This WhatsApp execution cannot be replayed. Review its current durable state.'
        });
      }
      return fail(400, {
        actionError: 'The WhatsApp execution request was rejected before it could be queued.'
      });
    }
  },
  testWhatsAppConnection: async ({ cookies }) => {
    const result = await localConnectionTestAction('whatsapp', cookies);
    if (!result) {
      return fail(502, { actionError: 'Could not run the local WhatsApp configuration check.' });
    }
    return { whatsappConfigTest: result };
  },
  testLinkedInConnection: async ({ cookies }) => {
    const result = await localConnectionTestAction('linkedin', cookies);
    if (!result) {
      return fail(502, { actionError: 'Could not run the local LinkedIn configuration check.' });
    }
    return { linkedinConfigTest: result };
  },
  testApolloConnection: async ({ cookies }) => {
    const result = await localConnectionTestAction('apollo', cookies);
    if (!result) {
      return fail(502, { actionError: 'Could not run the local Apollo configuration check.' });
    }
    return { apolloConfigTest: result };
  },
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
      linkedin_invitation_message: String(formData.get('linkedin_invitation_message') || '').trim(),
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
      return fixedOutboundFailure(err, 'Could not save the outbound campaign.');
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
      const connection = normalizeWhatsAppConnection(
        await apiRequest(
          '/integrations/whatsapp/connection/',
          { method: 'PUT', body },
          { cookies, org: locals?.org }
        )
      );
      return { whatsappSaved: true, whatsappConnection: connection };
    } catch (err) {
      return fixedOutboundFailure(err, 'Could not save the WhatsApp connection.');
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
      return fixedOutboundFailure(err, 'Could not save the LinkedIn connection.');
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
      return fixedOutboundFailure(err, 'Could not save the Apollo connection.');
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
      return fixedOutboundFailure(err, 'Could not save the Apollo source.');
    }
  },
  syncApolloSource: async ({ request, cookies, locals }) => {
    const formData = await request.formData();
    const sourceId = String(formData.get('source_id') || '');
    if (!validApolloUuid(sourceId)) {
      return fail(400, { actionError: 'The Apollo source is invalid.' });
    }
    const approvalValue = String(formData.get('approval_id') || '').trim();
    const executionBody = approvalValue
      ? buildApolloApprovedExecution(approvalValue, () => crypto.randomUUID())
      : {};
    if (approvalValue && !executionBody) {
      return fail(400, { actionError: 'Enter a valid one-time approval UUID.' });
    }
    try {
      const result = await apiRequest(
        `/sdr/outbound/sources/${sourceId}/sync/`,
        { method: 'POST', body: executionBody },
        { cookies, org: locals?.org }
      );
      if (!approvalValue) {
        const intent = normalizeApolloApprovalRequired(
          result,
          'search_people',
          `outbound-source:${sourceId}`
        );
        if (!intent) {
          return fail(502, {
            actionError: 'Apollo returned an unexpected approval-intent response.'
          });
        }
        return {
          apolloSearchApprovalRequired: true,
          apolloSourceId: sourceId,
          apolloSearchIntent: intent
        };
      }
      if (!validApolloUuid(result?.job_id)) {
        return fail(502, { actionError: 'Apollo did not return a valid queued job.' });
      }
      return { apolloSourceQueued: true, apolloSourceId: sourceId };
    } catch (err) {
      return fixedOutboundFailure(err, 'Could not queue the Apollo source sync.');
    }
  },
  prepareApolloCandidateEnrichment: async ({ request, cookies, locals }) => {
    const formData = await request.formData();
    const sourceId = String(formData.get('source_id') || '');
    const candidateId = String(formData.get('candidate_id') || '');
    if (!validApolloUuid(sourceId) || !validApolloUuid(candidateId)) {
      return fail(400, { actionError: 'The Apollo candidate is invalid.' });
    }
    try {
      const result = await apiRequest(
        `/sdr/outbound/apollo-candidates/${candidateId}/enrich/`,
        { method: 'POST', body: {} },
        { cookies, org: locals?.org }
      );
      const intent = normalizeApolloApprovalRequired(
        result,
        'enrich_person',
        `apollo-candidate:${candidateId}`
      );
      if (!intent) {
        return fail(502, {
          actionError: 'Apollo returned an unexpected enrichment-intent response.'
        });
      }
      return {
        apolloEnrichmentApprovalRequired: true,
        apolloSourceId: sourceId,
        apolloCandidateId: candidateId,
        apolloEnrichmentIntent: intent
      };
    } catch (err) {
      return fixedOutboundFailure(err, 'Could not prepare the Apollo enrichment approval.');
    }
  },
  enrichApolloCandidate: async ({ request, cookies, locals }) => {
    const formData = await request.formData();
    const sourceId = String(formData.get('source_id') || '');
    const candidateId = String(formData.get('candidate_id') || '');
    const executionBody = buildApolloApprovedExecution(
      String(formData.get('approval_id') || '').trim(),
      () => crypto.randomUUID()
    );
    if (!validApolloUuid(sourceId) || !validApolloUuid(candidateId) || !executionBody) {
      return fail(400, { actionError: 'Candidate and one-time approval UUID are required.' });
    }
    try {
      const result = await apiRequest(
        `/sdr/outbound/apollo-candidates/${candidateId}/enrich/`,
        { method: 'POST', body: executionBody },
        { cookies, org: locals?.org }
      );
      if (!validApolloUuid(result?.job_id) || String(result?.candidate_id || '') !== candidateId) {
        return fail(502, { actionError: 'Apollo did not return a valid enrichment job.' });
      }
      return {
        apolloCandidateEnrichmentQueued: true,
        apolloSourceId: sourceId,
        apolloCandidateId: candidateId
      };
    } catch (err) {
      return fixedOutboundFailure(err, 'Could not queue the Apollo candidate enrichment.');
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
      return fixedOutboundFailure(err, 'Could not queue outbound copy generation.');
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
      return fixedOutboundFailure(err, 'Could not save the reviewed outbound copy.');
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
      return fixedOutboundFailure(err, 'Could not apply the outbound copy.');
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
      return fixedOutboundFailure(err, 'Could not update campaign execution.');
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
      return fixedOutboundFailure(err, 'Could not import the prospect list.');
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
      return fixedOutboundFailure(err, 'Could not update the prospect.');
    }
  }
};
