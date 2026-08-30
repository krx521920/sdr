import { apiRequest } from '$lib/api-helpers.js';
import { normalizeChannelSafety, safeChannel, validUuid } from '$lib/channel-safety.js';
import { logSafeServerError } from '$lib/server/safe-error-log.js';
import { error, fail } from '@sveltejs/kit';

function requireAdmin(profile) {
  if (profile?.role !== 'ADMIN' && profile?.is_organization_admin !== true)
    throw error(403, 'Administrator access required');
}
const number = (form, key, max) => {
  const value = Number(form.get(key));
  return Number.isInteger(value) && value >= 0 && value <= max ? value : null;
};
const fixedFailure = (err, fallback) => {
  logSafeServerError('Channel safety operation failed', err);
  const status = Number(err?.status);
  return fail(status === 403 ? 403 : status === 409 ? 409 : 400, {
    actionError:
      status === 403
        ? 'Your administrator permission changed.'
        : status === 409
          ? 'Safety settings changed. Refresh and review before retrying.'
          : fallback
  });
};

export async function load({ cookies, locals, url }) {
  requireAdmin(locals.profile);
  const requestedId = String(url.searchParams.get('request') || '');
  const focusedUnknownRequestId = validUuid(requestedId) ? requestedId : '';
  try {
    return {
      safety: normalizeChannelSafety(
        await apiRequest('/integrations/channel-safety/', {}, { cookies, org: locals?.org })
      ),
      focusedUnknownRequestId,
      loadError: ''
    };
  } catch (err) {
    logSafeServerError('Could not load channel safety controls', err);
    return {
      safety: normalizeChannelSafety({}),
      focusedUnknownRequestId,
      loadError: 'Could not load channel safety controls.'
    };
  }
}

export const actions = {
  saveOrganization: async ({ request, cookies, locals }) => {
    requireAdmin(locals.profile);
    const form = await request.formData();
    const daily = number(form, 'daily_limit', 10_000_000);
    const revision = number(form, 'expected_revision', Number.MAX_SAFE_INTEGER);
    if (daily === null || revision === null)
      return fail(400, { actionError: 'Enter a valid organization limit.' });
    try {
      await apiRequest(
        '/integrations/channel-safety/organization/',
        {
          method: 'PUT',
          body: { enabled: form.has('enabled'), daily_limit: daily, expected_revision: revision },
          headers: { 'Idempotency-Key': crypto.randomUUID() }
        },
        { cookies, org: locals?.org }
      );
      return { saved: true };
    } catch (err) {
      return fixedFailure(err, 'Could not save organization safety controls.');
    }
  },
  saveChannel: async ({ request, cookies, locals }) => {
    requireAdmin(locals.profile);
    const form = await request.formData();
    const channel = safeChannel(String(form.get('channel') || ''));
    const daily = number(form, 'daily_limit', 1_000_000);
    const single = number(form, 'per_execution_limit', 1_000_000);
    const revision = number(form, 'expected_revision', Number.MAX_SAFE_INTEGER);
    if (!channel || daily === null || single === null || revision === null)
      return fail(400, { actionError: 'Channel safety values are invalid.' });
    try {
      await apiRequest(
        `/integrations/channel-safety/channels/${channel}/`,
        {
          method: 'PUT',
          body: {
            enabled: form.has('enabled'),
            test_mode: form.has('test_mode'),
            daily_limit: daily,
            per_execution_limit: single,
            expected_revision: revision
          },
          headers: { 'Idempotency-Key': crypto.randomUUID() }
        },
        { cookies, org: locals?.org }
      );
      return { saved: true };
    } catch (err) {
      return fixedFailure(err, 'Could not save channel safety controls.');
    }
  },
  addTarget: async ({ request, cookies, locals }) => {
    requireAdmin(locals.profile);
    const form = await request.formData();
    const channel = safeChannel(String(form.get('channel') || ''));
    const identifier = String(form.get('identifier') || '').trim();
    const safeLabel = String(form.get('safe_label') || '')
      .trim()
      .slice(0, 120);
    if (!channel || !identifier || identifier.length > 1000 || !safeLabel)
      return fail(400, { actionError: 'Provide a valid test target and masked label.' });
    try {
      await apiRequest(
        '/integrations/channel-safety/test-targets/',
        {
          method: 'POST',
          body: { channel, identifier, safe_label: safeLabel },
          headers: { 'Idempotency-Key': crypto.randomUUID() }
        },
        { cookies, org: locals?.org }
      );
      return { targetSaved: true };
    } catch (err) {
      return fixedFailure(err, 'Could not add the test target.');
    }
  },
  disableTarget: async ({ request, cookies, locals }) => {
    requireAdmin(locals.profile);
    const form = await request.formData();
    const id = String(form.get('target_id') || '');
    if (!validUuid(id)) return fail(400, { actionError: 'Invalid test target.' });
    try {
      await apiRequest(
        `/integrations/channel-safety/test-targets/${id}/`,
        { method: 'DELETE', headers: { 'Idempotency-Key': crypto.randomUUID() } },
        { cookies, org: locals?.org }
      );
      return { targetSaved: true };
    } catch (err) {
      return fixedFailure(err, 'Could not disable the test target.');
    }
  },
  approve: async ({ request, cookies, locals }) => {
    requireAdmin(locals.profile);
    const form = await request.formData();
    const target = String(form.get('target_id') || '');
    const hash = String(form.get('payload_sha256') || '')
      .trim()
      .toLowerCase();
    const units = number(form, 'units', 1_000_000);
    const expiry = number(form, 'expires_in_seconds', 86400);
    const action = String(form.get('approval_action') || '').trim();
    if (
      !validUuid(target) ||
      !/^[0-9a-f]{64}$/.test(hash) ||
      !/^[a-z][a-z0-9_.:-]{0,63}$/.test(action) ||
      !units ||
      expiry === null ||
      expiry < 60
    )
      return fail(400, { actionError: 'Approval details are invalid.' });
    try {
      const approval = await apiRequest(
        '/integrations/channel-safety/approvals/',
        {
          method: 'POST',
          body: {
            target_id: target,
            action,
            payload_sha256: hash,
            units,
            expires_in_seconds: expiry
          },
          headers: { 'Idempotency-Key': crypto.randomUUID() }
        },
        { cookies, org: locals?.org }
      );
      if (!validUuid(approval?.id))
        return fail(502, { actionError: 'Approval service returned an invalid response.' });
      return { approvalSaved: true, issuedApprovalId: String(approval.id) };
    } catch (err) {
      return fixedFailure(err, 'Could not create the one-time approval.');
    }
  },
  resolveUnknown: async ({ request, cookies, locals }) => {
    requireAdmin(locals.profile);
    const form = await request.formData();
    const id = String(form.get('request_id') || '');
    const outcome = String(form.get('outcome') || '');
    if (!validUuid(id) || !['delivered', 'failed_consumed'].includes(outcome))
      return fail(400, { actionError: 'Reconciliation decision is invalid.' });
    try {
      await apiRequest(
        `/integrations/channel-safety/unknown/${id}/resolve/`,
        { method: 'POST', body: { outcome }, headers: { 'Idempotency-Key': crypto.randomUUID() } },
        { cookies, org: locals?.org }
      );
      return { reconciled: true };
    } catch (err) {
      return fixedFailure(err, 'Could not reconcile the request.');
    }
  }
};
