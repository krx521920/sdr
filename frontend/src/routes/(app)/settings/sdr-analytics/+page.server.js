import { apiRequest } from '$lib/api-helpers.js';
import { error } from '@sveltejs/kit';

const VALID_DAYS = new Set([7, 30, 90]);

function emptyAnalytics(days) {
  return {
    period: { days },
    kpis: {
      received: { value: 0, previous: 0, change_percent: null },
      mql: { value: 0, previous: 0, change_percent: null },
      sql: { value: 0, previous: 0, change_percent: null },
      mql_rate: { value: 0, previous: 0 },
      mql_to_sql_rate: { value: 0, previous: 0 }
    },
    funnel: [],
    sources: [],
    trend: [],
    engagement: { sent: 0, variants: [] },
    response_sla: { sample_size: 0, within_sla_rate: 0 },
    sales_feedback: {
      summary: {
        total: 0,
        accepted: 0,
        rejected: 0,
        recycled: 0,
        coverage_rate: 0,
        acceptance_rate: 0,
        calibration_ready: false,
        minimum_calibration_samples: 10
      },
      rejection_reasons: [],
      by_qualification_band: [],
      by_model: []
    },
    insights: [],
    definitions: {}
  };
}

/** @type {import('./$types').PageServerLoad} */
export async function load({ cookies, locals, url }) {
  const profile = locals.profile;
  if (profile?.role !== 'ADMIN' && !profile?.is_organization_admin) {
    throw error(403, 'Only admins can view SDR growth analytics');
  }

  const requestedDays = Number(url.searchParams.get('days') || 30);
  const days = VALID_DAYS.has(requestedDays) ? requestedDays : 30;
  try {
    const analytics = await apiRequest(
      `/sdr/analytics/funnel/?days=${days}`,
      {},
      { cookies, org: locals?.org }
    );
    return { analytics, selectedDays: days, loadError: '' };
  } catch (err) {
    console.error('Failed to load SDR analytics:', err);
    return {
      analytics: emptyAnalytics(days),
      selectedDays: days,
      loadError: err?.message || 'Could not load SDR growth analytics.'
    };
  }
}
