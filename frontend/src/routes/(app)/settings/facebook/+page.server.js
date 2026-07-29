import { apiRequest } from '$lib/api-helpers.js';
import { fail, redirect } from '@sveltejs/kit';

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

/** @type {import('./$types').PageServerLoad} */
export async function load({ cookies, url }) {
  let connections = [];
  let connectionError = '';
  let oauthSession = null;

  try {
    connections = await apiRequest('/integrations/facebook/pages/', {}, { cookies });
  } catch (error) {
    connectionError = error?.message || 'Could not load Facebook Page connections.';
  }

  const sessionId = url.searchParams.get('facebook_oauth_session') || '';
  if (UUID_PATTERN.test(sessionId)) {
    try {
      oauthSession = await apiRequest(
        `/integrations/facebook/oauth/sessions/${sessionId}/`,
        {},
        { cookies }
      );
    } catch (error) {
      connectionError = error?.message || 'Could not load the Facebook authorization.';
    }
  }

  return {
    connections,
    connectionError,
    oauthSession,
    oauthError: url.searchParams.get('facebook_oauth_error') || '',
    connected: url.searchParams.get('connected') === '1'
  };
}

/** @type {import('./$types').Actions} */
export const actions = {
  startOAuth: async ({ cookies }) => {
    let result;
    try {
      result = await apiRequest(
        '/integrations/facebook/oauth/start/',
        { method: 'POST' },
        { cookies }
      );
    } catch (error) {
      return fail(400, {
        actionError: error?.message || 'Could not start Facebook authorization.'
      });
    }
    throw redirect(303, result.authorization_url);
  },

  selectPages: async ({ request, cookies }) => {
    const formData = await request.formData();
    const sessionId = formData.get('session_id')?.toString() || '';
    const pageIds = formData.getAll('page_ids').map((value) => value.toString());

    if (!UUID_PATTERN.test(sessionId) || pageIds.length === 0) {
      return fail(400, { actionError: 'Select at least one Facebook Page.' });
    }

    try {
      await apiRequest(
        `/integrations/facebook/oauth/sessions/${sessionId}/select/`,
        { method: 'POST', body: { page_ids: pageIds } },
        { cookies }
      );
    } catch (error) {
      return fail(400, {
        actionError: error?.message || 'Could not connect the selected Facebook Pages.'
      });
    }
    throw redirect(303, '/settings/facebook?connected=1');
  },

  disconnect: async ({ request, cookies }) => {
    const formData = await request.formData();
    const connectionId = formData.get('connection_id')?.toString() || '';
    if (!UUID_PATTERN.test(connectionId)) {
      return fail(400, { actionError: 'The Facebook Page connection is invalid.' });
    }

    try {
      await apiRequest(
        `/integrations/facebook/pages/${connectionId}/`,
        { method: 'DELETE' },
        { cookies }
      );
      return { disconnected: true };
    } catch (error) {
      return fail(400, {
        actionError: error?.message || 'Could not disconnect the Facebook Page.'
      });
    }
  }
};
