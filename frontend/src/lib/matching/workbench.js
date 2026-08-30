const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export const OPPORTUNITY_STATUSES = ['draft', 'open', 'paused', 'filled', 'closed'];
export const OPPORTUNITY_TYPES = [
  'customer',
  'employment',
  'contractor',
  'project',
  'expert',
  'referral',
  'partnership'
];
export const MATCH_STATUSES = [
  'proposed',
  'reviewing',
  'shortlisted',
  'accepted',
  'rejected',
  'expired'
];
export const DECISION_STATUSES = ['proposed', 'reviewing', 'shortlisted', 'accepted', 'rejected'];
export const MATCH_RUN_ACTIVE_STATUSES = ['pending', 'queued', 'running', 'retry_scheduled'];
export const MATCH_RUN_TERMINAL_STATUSES = ['succeeded', 'failed', 'dead_letter', 'cancelled'];

const CREATE_OPPORTUNITY_STATUSES = new Set(['draft', 'open']);
const CREATE_OPPORTUNITY_REMOTE_MODES = new Set(['', 'on_site', 'hybrid', 'remote']);
const CREATE_CRITERIA_FIELDS = [
  ['required_skills', 'required_criteria', 'skills'],
  ['required_titles', 'required_criteria', 'titles'],
  ['required_locations', 'required_criteria', 'locations'],
  ['preferred_skills', 'preferred_criteria', 'skills']
];

const DECISION_TARGETS = {
  proposed: ['reviewing', 'shortlisted', 'rejected'],
  reviewing: ['shortlisted', 'rejected'],
  shortlisted: ['reviewing', 'accepted', 'rejected'],
  accepted: [],
  rejected: [],
  expired: []
};

/**
 * Capabilities are authorization hints rendered by the UI, never a substitute
 * for the API's permission checks. Fail closed unless the API returns literal
 * booleans.
 *
 * @param {unknown} raw
 */
export function normalizeMatchingCapabilities(raw) {
  const value = /** @type {any} */ (
    raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : {}
  );
  return {
    read: value.read === true,
    manage: value.manage === true,
    recompute: value.recompute === true,
    decide: value.decide === true,
    export: value.export === true,
    delete: value.delete === true,
    retention: value.retention === true,
    feedback: value.feedback === true,
    calibrate: value.calibrate === true
  };
}

/** @param {unknown} value */
export function isUuid(value) {
  return typeof value === 'string' && UUID_PATTERN.test(value);
}

/**
 * Convert the bounded, human-readable create form into the matching API
 * contract. The workbench deliberately does not accept arbitrary JSON from a
 * browser form.
 *
 * @param {{ get(name: string): unknown }} form
 * @returns {{ payload?: Record<string, unknown>, error?: string }}
 */
export function buildCreateOpportunityPayload(form) {
  if (!form || typeof form.get !== 'function') {
    return { error: 'The opportunity form is invalid.' };
  }

  const text = (name) => String(form.get(name) || '').trim();
  const title = text('title');
  const opportunityType = text('opportunity_type').toLowerCase();
  const status = text('status').toLowerCase();
  const description = text('description');
  const organizationName = text('organization_name');
  const location = text('location');
  const remoteMode = text('remote_mode').toLowerCase();

  if (!title || title.length > 255) {
    return { error: 'Enter an opportunity title of at most 255 characters.' };
  }
  if (!OPPORTUNITY_TYPES.includes(opportunityType)) {
    return { error: 'Select a valid opportunity type.' };
  }
  if (!CREATE_OPPORTUNITY_STATUSES.has(status)) {
    return { error: 'Select draft or open as the initial status.' };
  }
  if (description.length > 5000) {
    return { error: 'Keep the opportunity description within 5,000 characters.' };
  }
  if (organizationName.length > 255 || location.length > 255) {
    return { error: 'Organization and location must each be at most 255 characters.' };
  }
  if (!CREATE_OPPORTUNITY_REMOTE_MODES.has(remoteMode)) {
    return { error: 'Select a valid work arrangement.' };
  }

  const criteria = {
    required_criteria: {},
    preferred_criteria: {},
    exclusion_criteria: {}
  };
  for (const [field, group, dimension] of CREATE_CRITERIA_FIELDS) {
    const parsed = parseCriteriaTerms(text(field));
    if (parsed.error) return { error: parsed.error };
    if (parsed.terms.length > 0) criteria[group][dimension] = parsed.terms;
  }

  return {
    payload: {
      opportunity_type: opportunityType,
      status,
      title,
      description,
      organization_name: organizationName,
      location,
      remote_mode: remoteMode,
      ...criteria
    }
  };
}

/** @param {string} raw */
function parseCriteriaTerms(raw) {
  if (raw.length > 4000) {
    return { terms: [], error: 'Keep each criteria field within 4,000 characters.' };
  }
  const terms = [];
  const seen = new Set();
  for (const value of raw.split(/[,;\n]/)) {
    const term = value.trim();
    if (!term) continue;
    if (term.length > 120) {
      return { terms: [], error: 'Each criterion must be at most 120 characters.' };
    }
    const key = term.toLocaleLowerCase('en');
    if (!seen.has(key)) {
      seen.add(key);
      terms.push(term);
    }
    if (terms.length > 50) {
      return { terms: [], error: 'Use at most 50 values in each criteria field.' };
    }
  }
  return { terms };
}

/**
 * Keep URL-controlled state bounded to values the matching API understands.
 *
 * @param {URLSearchParams} searchParams
 */
export function parseWorkbenchFilters(searchParams) {
  const status = String(searchParams.get('status') || '').toLowerCase();
  const type = String(searchParams.get('type') || '').toLowerCase();
  const matchStatus = String(searchParams.get('match_status') || '').toLowerCase();
  const opportunity = String(searchParams.get('opportunity') || '');
  const run = String(searchParams.get('run') || '');

  return {
    q: String(searchParams.get('q') || '')
      .trim()
      .slice(0, 100),
    status: OPPORTUNITY_STATUSES.includes(status) ? status : '',
    type: OPPORTUNITY_TYPES.includes(type) ? type : '',
    matchStatus: MATCH_STATUSES.includes(matchStatus) ? matchStatus : '',
    opportunity: isUuid(opportunity) ? opportunity : '',
    run: isUuid(run) ? run : ''
  };
}

/** @param {{ status?: string, type?: string, q?: string }} filters @param {number} [limit] */
export function buildOpportunityQuery(filters, limit = 100) {
  const params = new URLSearchParams();
  params.set('limit', String(Math.min(Math.max(Math.trunc(limit) || 100, 1), 500)));
  if (filters.status) params.set('status', filters.status);
  if (filters.type) params.set('type', filters.type);
  return params.toString();
}

/** @param {any} raw */
export function normalizeOpportunity(raw) {
  return {
    id: String(raw?.id || ''),
    type: String(raw?.opportunity_type || ''),
    status: String(raw?.status || ''),
    title: String(raw?.title || 'Untitled opportunity'),
    description: String(raw?.description || ''),
    organizationName: String(raw?.organization_name || ''),
    location: String(raw?.location || ''),
    remoteMode: String(raw?.remote_mode || ''),
    requiredCriteria: safeCriteria(raw?.required_criteria),
    preferredCriteria: safeCriteria(raw?.preferred_criteria),
    exclusionCriteria: safeCriteria(raw?.exclusion_criteria),
    scoringWeights: safeNumberRecord(raw?.scoring_weights),
    matchCount: safeInteger(raw?.match_count),
    openedAt: safeDateString(raw?.opened_at),
    closesAt: safeDateString(raw?.closes_at),
    createdAt: safeDateString(raw?.created_at),
    updatedAt: safeDateString(raw?.updated_at)
  };
}

/** @param {any[]} opportunities @param {string} query */
export function filterOpportunities(opportunities, query) {
  const needle = String(query || '')
    .trim()
    .toLowerCase();
  if (!needle) return opportunities;
  return opportunities.filter((opportunity) =>
    [opportunity.title, opportunity.organizationName, opportunity.location, opportunity.type]
      .filter(Boolean)
      .some((value) => String(value).toLowerCase().includes(needle))
  );
}

/** @param {any[]} opportunities @param {string} requestedId */
export function chooseOpportunity(opportunities, requestedId) {
  if (requestedId) {
    const requested = opportunities.find((opportunity) => opportunity.id === requestedId);
    if (requested) return requested;
  }
  return (
    opportunities.find((opportunity) => opportunity.status === 'open') || opportunities[0] || null
  );
}

/**
 * Reduce evidence to the explainability fields needed by the workbench. Raw
 * facts, source URIs, source record IDs and identities must never cross this
 * server-to-page boundary.
 *
 * @param {any[]} links
 */
export function sanitizeEvidenceLinks(links) {
  if (!Array.isArray(links)) return [];
  return links.map((link) => ({
    id: String(link?.id || ''),
    direction: String(link?.direction || 'neutral'),
    relevance: safeRatio(link?.relevance),
    contribution: safeNumber(link?.contribution),
    explanation: String(link?.explanation || '').slice(0, 500),
    evidence: {
      id: String(link?.evidence?.id || ''),
      kind: String(link?.evidence?.kind || 'other'),
      source: String(link?.evidence?.source || 'other'),
      summary: String(link?.evidence?.summary || '').slice(0, 1000),
      observedAt: safeDateString(link?.evidence?.observed_at),
      validUntil: safeDateString(link?.evidence?.valid_until),
      confidence: safeRatio(link?.evidence?.confidence),
      reviewStatus: ['pending', 'confirmed', 'rejected'].includes(
        String(link?.evidence?.review_status || '').toLowerCase()
      )
        ? String(link.evidence.review_status).toLowerCase()
        : '',
      freshness: ['active', 'expiring', 'expired'].includes(
        String(link?.evidence?.freshness || '').toLowerCase()
      )
        ? String(link.evidence.freshness).toLowerCase()
        : '',
      aiGenerated: link?.evidence?.ai_generated === true
    }
  }));
}

/** @param {any} raw */
export function normalizeMatch(raw) {
  const personSummary = {
    id: String(raw?.person_summary?.id || raw?.person || ''),
    displayName: String(raw?.person_summary?.display_name || raw?.person_name || 'Unnamed person'),
    currentTitle: String(raw?.person_summary?.current_title || ''),
    currentCompany: String(raw?.person_summary?.current_company || ''),
    location: String(raw?.person_summary?.location || ''),
    availability: String(raw?.person_summary?.availability || '')
  };
  return {
    id: String(raw?.id || ''),
    personId: String(raw?.person || ''),
    personName: personSummary.displayName,
    personSummary,
    status: MATCH_STATUSES.includes(raw?.status) ? raw.status : 'proposed',
    rank: safeInteger(raw?.rank),
    overallScore: safeScore(raw?.overall_score),
    eligibilityScore: safeScore(raw?.eligibility_score),
    fitScore: safeScore(raw?.fit_score),
    trustScore: safeScore(raw?.trust_score),
    relationshipScore: safeScore(raw?.relationship_score),
    availabilityScore: safeScore(raw?.availability_score),
    confidence: safeRatio(raw?.confidence),
    reasons: safeExplanations(raw?.reasons, 'matched'),
    gaps: safeExplanations(raw?.gaps, 'missing'),
    evaluatedAt: safeDateString(raw?.evaluated_at),
    engineVersion: String(raw?.engine_version || ''),
    rankingRevision: safeInteger(raw?.ranking_revision),
    decisionRevision: safeInteger(raw?.decision_revision),
    feedbackRevision: safeInteger(raw?.feedback_revision),
    recommendationVerdict: [
      'unknown',
      'accurate',
      'partially_accurate',
      'inaccurate',
      'uncertain'
    ].includes(String(raw?.recommendation_verdict || '').toLowerCase())
      ? String(raw.recommendation_verdict).toLowerCase()
      : 'unknown',
    latestOutcomeCode: String(raw?.latest_outcome_code || '').slice(0, 40),
    latestOutcomeAt: safeDateString(raw?.latest_outcome_at),
    scoringPolicyVersionId: isUuid(String(raw?.scoring_policy_version || ''))
      ? String(raw.scoring_policy_version)
      : '',
    decisionReason: String(raw?.decision_reason || '').slice(0, 1000),
    decidedAt: safeDateString(raw?.decided_at),
    evidenceLinks: sanitizeEvidenceLinks(raw?.evidence_links)
  };
}

/**
 * Keep the automation envelope out of the browser. Only the safe MatchRun
 * contract is copied, so payloads, raw results and error messages are dropped.
 *
 * @param {any} raw
 */
export function normalizeMatchRun(raw) {
  const totalCount = safeInteger(raw?.total_count);
  const processedCount = Math.min(
    totalCount || Number.MAX_SAFE_INTEGER,
    safeInteger(raw?.processed_count)
  );
  const status = String(raw?.status || '').toLowerCase();
  const outcome = String(raw?.outcome || '').toLowerCase();
  return {
    id: isUuid(String(raw?.id || '')) ? String(raw.id) : '',
    opportunityId: isUuid(String(raw?.opportunity || '')) ? String(raw.opportunity) : '',
    status: [...MATCH_RUN_ACTIVE_STATUSES, ...MATCH_RUN_TERMINAL_STATUSES].includes(status)
      ? status
      : '',
    outcome: ['succeeded', 'skipped'].includes(outcome) ? outcome : '',
    totalCount,
    processedCount,
    resultCount: safeInteger(raw?.result_count),
    progress: totalCount > 0 ? Math.min(100, Math.round((processedCount / totalCount) * 100)) : 0,
    rankingRevision: safeInteger(raw?.ranking_revision),
    engineVersion: String(raw?.engine_version || '').slice(0, 64),
    startedAt: safeDateString(raw?.started_at),
    completedAt: safeDateString(raw?.completed_at),
    errorCode: safeErrorCode(raw?.error_code),
    createdAt: safeDateString(raw?.created_at),
    updatedAt: safeDateString(raw?.updated_at)
  };
}

/** @param {any} run */
export function isMatchRunActive(run) {
  return Boolean(run?.id) && MATCH_RUN_ACTIVE_STATUSES.includes(run.status);
}

/** @param {any} run */
export function isMatchRunTerminal(run) {
  return (
    Boolean(run?.id) &&
    (MATCH_RUN_TERMINAL_STATUSES.includes(run.status) ||
      ['succeeded', 'skipped'].includes(run.outcome))
  );
}

/** @param {any} run */
export function isMatchRunSuccessful(run) {
  return (
    Boolean(run?.id) &&
    run.outcome !== 'skipped' &&
    (run.status === 'succeeded' || run.outcome === 'succeeded')
  );
}

/** @param {any} run */
export function isMatchRunSkipped(run) {
  return Boolean(run?.id) && run.outcome === 'skipped';
}

/** @param {unknown} value */
export function isDecisionStatus(value) {
  return typeof value === 'string' && DECISION_STATUSES.includes(value);
}

/** @param {unknown} currentStatus */
export function decisionTargetsForStatus(currentStatus) {
  return [...(DECISION_TARGETS[String(currentStatus)] || [])];
}

/** @param {unknown} value */
export function scoreLabel(value) {
  const score = safeScore(value);
  if (score >= 80) return 'Strong fit';
  if (score >= 60) return 'Good fit';
  if (score >= 50) return 'Possible fit';
  return 'Weak fit';
}

/** @param {unknown} value */
function safeNumber(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number : 0;
}

/** @param {unknown} value */
function safeInteger(value) {
  return Math.max(0, Math.trunc(safeNumber(value)));
}

/** @param {unknown} value */
function safeScore(value) {
  return Math.min(100, safeInteger(value));
}

/** @param {unknown} value */
function safeRatio(value) {
  return Math.min(1, Math.max(0, safeNumber(value)));
}

/** @param {unknown} value */
function safeDateString(value) {
  return typeof value === 'string' ? value : '';
}

/** @param {unknown} value */
function safeErrorCode(value) {
  return String(value || '')
    .replace(/[^a-z0-9_.:-]/gi, '')
    .slice(0, 80);
}

/** @param {unknown} value */
function safeCriteria(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
  return Object.fromEntries(
    Object.entries(value)
      .filter(([, items]) => Array.isArray(items))
      .map(([key, items]) => [
        key,
        items.filter((item) => typeof item === 'string').map((item) => item.slice(0, 120))
      ])
  );
}

/** @param {unknown} value */
function safeNumberRecord(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
  return Object.fromEntries(
    Object.entries(value)
      .filter(([, number]) => Number.isFinite(Number(number)))
      .map(([key, number]) => [key, Number(number)])
  );
}

/** @param {unknown} value @param {string} listKey */
function safeExplanations(value, listKey) {
  if (!Array.isArray(value)) return [];
  return value.map((item) => ({
    dimension: String(item?.dimension || ''),
    message: String(item?.message || '').slice(0, 500),
    terms: Array.isArray(item?.[listKey])
      ? item[listKey].filter((term) => typeof term === 'string').map((term) => term.slice(0, 120))
      : []
  }));
}
