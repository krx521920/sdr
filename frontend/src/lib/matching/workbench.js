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

/** @param {unknown} value */
export function isUuid(value) {
  return typeof value === 'string' && UUID_PATTERN.test(value);
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

  return {
    q: String(searchParams.get('q') || '')
      .trim()
      .slice(0, 100),
    status: OPPORTUNITY_STATUSES.includes(status) ? status : '',
    type: OPPORTUNITY_TYPES.includes(type) ? type : '',
    matchStatus: MATCH_STATUSES.includes(matchStatus) ? matchStatus : '',
    opportunity: isUuid(opportunity) ? opportunity : ''
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
      confidence: safeRatio(link?.evidence?.confidence)
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
    evidenceLinks: sanitizeEvidenceLinks(raw?.evidence_links)
  };
}

/** @param {unknown} value */
export function isDecisionStatus(value) {
  return typeof value === 'string' && DECISION_STATUSES.includes(value);
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
