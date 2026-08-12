import { apiRequest } from '$lib/api-helpers.js';
import { fail, redirect } from '@sveltejs/kit';

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

/** @type {import('./$types').PageServerLoad} */
export async function load({ cookies, url }) {
  let connections = [];
  let conversion = null;
  let connectionError = '';
  let oauthSession = null;

  try {
    connections = await apiRequest('/integrations/facebook/pages/', {}, { cookies });
  } catch (error) {
    connectionError = error?.message || 'Could not load Facebook Page connections.';
  }

  try {
    conversion = await apiRequest('/integrations/facebook/conversions/', {}, { cookies });
  } catch (error) {
    connectionError = error?.message || 'Could not load Meta conversion feedback settings.';
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
    conversion,
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
  },

  toggleMessenger: async ({ request, cookies }) => {
    const formData = await request.formData();
    const connectionId = formData.get('connection_id')?.toString() || '';
    const messengerEnabled = formData.get('messenger_enabled')?.toString() === 'true';

    if (!UUID_PATTERN.test(connectionId)) {
      return fail(400, { actionError: 'The Facebook Page connection is invalid.' });
    }

    try {
      await apiRequest(
        `/integrations/facebook/pages/${connectionId}/`,
        { method: 'PATCH', body: { messenger_enabled: messengerEnabled } },
        { cookies }
      );
      return { messengerUpdated: true, messengerEnabled };
    } catch (error) {
      return fail(400, {
        actionError:
          error?.message ||
          'Could not update Messenger intake. Reconnect Facebook if Page messaging permission is missing.'
      });
    }
  },

  saveMessengerReply: async ({ request, cookies }) => {
    const formData = await request.formData();
    const connectionId = formData.get('connection_id')?.toString() || '';
    const autoReplyTemplate =
      formData.get('messenger_auto_reply_template')?.toString().trim() || '';

    if (!UUID_PATTERN.test(connectionId)) {
      return fail(400, { actionError: 'The Facebook Page connection is invalid.' });
    }
    if (!autoReplyTemplate) {
      return fail(400, { actionError: 'Enter a Messenger auto-reply message.' });
    }

    try {
      await apiRequest(
        `/integrations/facebook/pages/${connectionId}/`,
        {
          method: 'PATCH',
          body: {
            messenger_auto_reply_enabled: formData.get('messenger_auto_reply_enabled') === 'on',
            messenger_auto_reply_template: autoReplyTemplate
          }
        },
        { cookies }
      );
      return { messengerReplySaved: true };
    } catch (error) {
      return fail(400, {
        actionError: error?.message || 'Could not save Messenger auto-reply settings.'
      });
    }
  },

  saveConversions: async ({ request, cookies }) => {
    const formData = await request.formData();
    const payload = {
      is_enabled: formData.get('is_enabled') === 'on',
      pixel_id: formData.get('pixel_id')?.toString().trim() || '',
      access_token: formData.get('access_token')?.toString().trim() || '',
      lead_event_source: formData.get('lead_event_source')?.toString().trim() || 'BottleCRM',
      raw_lead_event_name: formData.get('raw_lead_event_name')?.toString().trim() || 'RawLead',
      qualified_lead_event_name:
        formData.get('qualified_lead_event_name')?.toString().trim() || 'MarketingQualifiedLead',
      converted_event_name: formData.get('converted_event_name')?.toString().trim() || 'Converted',
      qualified_bands: formData.getAll('qualified_bands').map((value) => value.toString()),
      test_event_code: formData.get('test_event_code')?.toString().trim() || ''
    };

    try {
      const conversion = await apiRequest(
        '/integrations/facebook/conversions/',
        { method: 'PUT', body: payload },
        { cookies }
      );
      return { conversionSaved: true, backfilledEvents: conversion.backfilled_events || 0 };
    } catch (error) {
      return fail(400, {
        actionError: error?.message || 'Could not save Meta conversion feedback settings.'
      });
    }
  }
};
