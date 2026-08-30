import { apiRequest } from '$lib/api-helpers.js';
import { error, fail } from '@sveltejs/kit';

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

function requireAdmin(profile) {
  if (profile?.role !== 'ADMIN' && !profile?.is_organization_admin) {
    throw error(403, 'Only admins can manage SDR compliance');
  }
}

/** @type {import('./$types').PageServerLoad} */
export async function load({ cookies, locals }) {
  requireAdmin(locals.profile);
  try {
    const [overview, rules, dnc, provenance] = await Promise.all([
      apiRequest('/sdr/compliance/', {}, { cookies, org: locals?.org }),
      apiRequest('/sdr/compliance/rules/', {}, { cookies, org: locals?.org }),
      apiRequest('/sdr/compliance/dnc/?limit=100', {}, { cookies, org: locals?.org }),
      apiRequest('/sdr/compliance/provenance/?limit=100', {}, { cookies, org: locals?.org })
    ]);
    return { overview, rules, dnc, provenance, loadError: '' };
  } catch (err) {
    return {
      overview: { settings: {}, summary: {}, choices: {}, recent_events: [] },
      rules: { results: [] },
      dnc: { results: [] },
      provenance: { results: [] },
      loadError: err?.message || 'Could not load SDR compliance controls.'
    };
  }
}

/** @type {import('./$types').Actions} */
export const actions = {
  saveSettings: async ({ request, cookies, locals }) => {
    requireAdmin(locals.profile);
    const form = await request.formData();
    try {
      await apiRequest(
        '/sdr/compliance/settings/',
        {
          method: 'PATCH',
          body: {
            enforcement_enabled: form.has('enforcement_enabled'),
            require_lawful_basis: form.has('require_lawful_basis'),
            retention_mode: String(form.get('retention_mode') || 'audit_only'),
            retention_days: Number(form.get('retention_days') || 730),
            deletion_grace_days: Number(form.get('deletion_grace_days') || 30)
          }
        },
        { cookies, org: locals?.org }
      );
      return { settingsSaved: true };
    } catch (err) {
      return fail(400, { actionError: err?.message || 'Could not save compliance settings.' });
    }
  },
  saveRule: async ({ request, cookies, locals }) => {
    requireAdmin(locals.profile);
    const form = await request.formData();
    const ruleId = String(form.get('rule_id') || '');
    if (ruleId && !UUID_PATTERN.test(ruleId)) return fail(400, { actionError: 'Invalid rule.' });
    try {
      await apiRequest(
        ruleId ? `/sdr/compliance/rules/${ruleId}/` : '/sdr/compliance/rules/',
        {
          method: ruleId ? 'PATCH' : 'POST',
          body: {
            country_code: String(form.get('country_code') || '*')
              .trim()
              .toUpperCase(),
            channel: String(form.get('channel') || 'email'),
            is_allowed: form.has('is_allowed'),
            requires_consent: form.has('requires_consent'),
            notes: String(form.get('notes') || '').trim()
          }
        },
        { cookies, org: locals?.org }
      );
      return { ruleSaved: true };
    } catch (err) {
      return fail(400, { actionError: err?.message || 'Could not save the rule.' });
    }
  },
  deleteRule: async ({ request, cookies, locals }) => {
    requireAdmin(locals.profile);
    const form = await request.formData();
    const ruleId = String(form.get('rule_id') || '');
    if (!UUID_PATTERN.test(ruleId)) return fail(400, { actionError: 'Invalid rule.' });
    try {
      await apiRequest(
        `/sdr/compliance/rules/${ruleId}/`,
        { method: 'DELETE' },
        { cookies, org: locals?.org }
      );
      return { ruleDeleted: true };
    } catch (err) {
      return fail(400, { actionError: err?.message || 'Could not delete the rule.' });
    }
  },
  addDnc: async ({ request, cookies, locals }) => {
    requireAdmin(locals.profile);
    const form = await request.formData();
    try {
      await apiRequest(
        '/sdr/compliance/dnc/',
        {
          method: 'POST',
          body: {
            channel: String(form.get('channel') || 'email'),
            identifier: String(form.get('identifier') || '').trim(),
            country_code: String(form.get('country_code') || '')
              .trim()
              .toUpperCase(),
            reason: String(form.get('reason') || 'admin')
          }
        },
        { cookies, org: locals?.org }
      );
      return { dncAdded: true };
    } catch (err) {
      return fail(400, { actionError: err?.message || 'Could not add the contact block.' });
    }
  },
  releaseDnc: async ({ request, cookies, locals }) => {
    requireAdmin(locals.profile);
    const form = await request.formData();
    const entryId = String(form.get('entry_id') || '');
    if (!UUID_PATTERN.test(entryId)) return fail(400, { actionError: 'Invalid DNC entry.' });
    try {
      await apiRequest(
        `/sdr/compliance/dnc/${entryId}/`,
        { method: 'DELETE' },
        { cookies, org: locals?.org }
      );
      return { dncReleased: true };
    } catch (err) {
      return fail(400, { actionError: err?.message || 'Could not release the contact block.' });
    }
  },
  updateProvenance: async ({ request, cookies, locals }) => {
    requireAdmin(locals.profile);
    const form = await request.formData();
    const intakeId = String(form.get('intake_id') || '');
    if (!UUID_PATTERN.test(intakeId)) return fail(400, { actionError: 'Invalid intake.' });
    try {
      await apiRequest(
        `/sdr/compliance/provenance/${intakeId}/`,
        {
          method: 'PATCH',
          body: {
            lawful_basis: String(form.get('lawful_basis') || 'unassessed'),
            lawful_basis_notes: String(form.get('lawful_basis_notes') || '').trim(),
            consent_at: String(form.get('consent_at') || '') || null,
            consent_evidence: String(form.get('consent_evidence') || '').trim(),
            allowed_channels: form.getAll('allowed_channels').map(String)
          }
        },
        { cookies, org: locals?.org }
      );
      return { provenanceSaved: true };
    } catch (err) {
      return fail(400, { actionError: err?.message || 'Could not update provenance.' });
    }
  },
  deletion: async ({ request, cookies, locals }) => {
    requireAdmin(locals.profile);
    const form = await request.formData();
    const intakeId = String(form.get('intake_id') || '');
    const action = String(form.get('deletion_action') || 'request');
    if (!UUID_PATTERN.test(intakeId)) return fail(400, { actionError: 'Invalid intake.' });
    const body = { action };
    if (action === 'anonymize')
      body.confirm_intake_id = String(form.get('confirm_intake_id') || '');
    try {
      await apiRequest(
        `/sdr/compliance/intakes/${intakeId}/deletion/`,
        { method: 'POST', body },
        { cookies, org: locals?.org }
      );
      return { deletionUpdated: true };
    } catch (err) {
      return fail(400, { actionError: err?.message || 'Could not update the deletion request.' });
    }
  },
  scanRetention: async ({ request, cookies, locals }) => {
    requireAdmin(locals.profile);
    const form = await request.formData();
    try {
      const result = await apiRequest(
        '/sdr/compliance/retention/scan/',
        { method: 'POST', body: { execute: form.has('execute'), limit: 200 } },
        { cookies, org: locals?.org }
      );
      return { retentionResult: result };
    } catch (err) {
      return fail(400, { actionError: err?.message || 'Could not run the retention scan.' });
    }
  }
};
