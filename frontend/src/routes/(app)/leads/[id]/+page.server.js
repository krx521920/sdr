/**
 * Lead Detail Page - Server Load
 *
 * Django endpoint: GET /api/leads/<id>/
 * Response shape: { lead_obj, attachments, comments, users_mention, assigned_data,
 *                   users, users_excluding_team, source, status, teams, countries }
 * (see backend/leads/views/lead_views.py LeadDetailView.get_context_data)
 */

import { error, fail } from '@sveltejs/kit';
import { apiRequest } from '$lib/api-helpers.js';
import { randomUUID } from 'node:crypto';

/** @type {import('./$types').PageServerLoad} */
export async function load({ params, locals, cookies }) {
  const org = locals.org;
  if (!org) {
    throw error(401, 'Organization context required');
  }

  try {
    const response = await apiRequest(`/leads/${params.id}/`, {}, { cookies, org });

    if (response?.error) {
      throw error(404, response.errors || 'Lead not found');
    }

    // Django LeadDetailView returns the lead under `lead_obj` (see backend/leads/views/lead_views.py).
    const lead = response.lead_obj || response.lead || response;
    let messengerConversation = { available: false, messages: [], can_reply: false };
    let messengerConversationError = '';
    let salesFeedback = { available: false };
    let salesFeedbackError = '';
    try {
      messengerConversation = await apiRequest(
        `/integrations/facebook/conversations/leads/${params.id}/`,
        {},
        { cookies, org }
      );
    } catch (err) {
      console.error('Failed to load Messenger conversation:', err);
      messengerConversationError =
        /** @type {any} */ (err)?.message || 'Failed to load Messenger conversation';
    }
    try {
      salesFeedback = await apiRequest(
        `/sdr/sales-feedback/leads/${params.id}/`,
        {},
        { cookies, org }
      );
    } catch (err) {
      console.error('Failed to load SDR sales feedback:', err);
      salesFeedbackError = /** @type {any} */ (err)?.message || 'Failed to load SDR sales feedback';
    }

    return {
      lead,
      comments: response.comments || [],
      attachments: response.attachments || [],
      tags: response.tags || lead?.tags || [],
      users: response.users || [],
      commentPermission: response.comment_permission || false,
      customFieldDefinitions: response.custom_field_definitions || [],
      customFieldValues: lead?.custom_fields || {},
      messengerConversation,
      messengerConversationError,
      messengerRequestId: randomUUID(),
      salesFeedback,
      salesFeedbackError
    };
  } catch (err) {
    if (/** @type {any} */ (err)?.status) throw err;
    console.error('Failed to load lead detail:', err);
    throw error(500, 'Failed to load lead');
  }
}

/** @type {import('./$types').Actions} */
export const actions = {
  updateCustomFields: async ({ request, params, locals, cookies }) => {
    const form = await request.formData();
    const raw = form.get('custom_fields')?.toString() || '{}';
    let parsed;
    try {
      parsed = JSON.parse(raw);
    } catch {
      return fail(400, { error: 'Malformed custom_fields payload' });
    }
    try {
      await apiRequest(
        `/leads/${params.id}/`,
        { method: 'PATCH', body: { custom_fields: parsed } },
        { cookies, org: locals.org }
      );
      return { success: true };
    } catch (err) {
      console.error('Update lead custom fields error:', err);
      return fail(400, {
        error: /** @type {any} */ (err)?.message || 'Failed to save custom fields'
      });
    }
  },

  sendMessengerReply: async ({ request, params, locals, cookies }) => {
    const form = await request.formData();
    const body = form.get('body')?.toString().trim() || '';
    const clientRequestId = form.get('client_request_id')?.toString() || '';
    if (!body) {
      return fail(400, { messengerError: 'Enter a reply message.' });
    }
    if (body.length > 2000) {
      return fail(400, { messengerError: 'Messenger replies cannot exceed 2,000 characters.' });
    }
    try {
      await apiRequest(
        `/integrations/facebook/conversations/leads/${params.id}/`,
        {
          method: 'POST',
          body: { body, client_request_id: clientRequestId }
        },
        { cookies, org: locals.org }
      );
      return { messengerReplyQueued: true };
    } catch (err) {
      console.error('Send Messenger reply error:', err);
      return fail(400, {
        messengerError: /** @type {any} */ (err)?.message || 'Failed to send Messenger reply'
      });
    }
  },

  saveSalesFeedback: async ({ request, params, locals, cookies }) => {
    const form = await request.formData();
    const payload = {
      decision: form.get('decision')?.toString() || '',
      reason: form.get('reason')?.toString() || '',
      quality_score: Number(form.get('quality_score')),
      satisfaction_score: Number(form.get('satisfaction_score')),
      notes: form.get('notes')?.toString().trim() || ''
    };
    try {
      await apiRequest(
        `/sdr/sales-feedback/leads/${params.id}/`,
        { method: 'PUT', body: payload },
        { cookies, org: locals.org }
      );
      return { salesFeedbackSaved: true };
    } catch (err) {
      console.error('Save SDR sales feedback error:', err);
      return fail(400, {
        salesFeedbackError: /** @type {any} */ (err)?.message || 'Failed to save SDR sales feedback'
      });
    }
  }
};
