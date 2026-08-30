const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export const GOVERNANCE_QUEUES = [
  'pending_ai',
  'expiring',
  'expired',
  'blocked',
  'deletion_requested'
];
export const EVIDENCE_REVIEW_STATUSES = ['pending', 'confirmed', 'rejected'];
export const EVIDENCE_FRESHNESS = ['active', 'expiring', 'expired'];
export const EVIDENCE_SOURCES = [
  'crm',
  'apollo',
  'linkedin',
  'whatsapp',
  'wechat',
  'feishu',
  'email',
  'manual',
  'ai',
  'other'
];
export const EVIDENCE_KINDS = [
  'profile',
  'skill',
  'experience',
  'relationship',
  'interaction',
  'availability',
  'preference',
  'verification',
  'other'
];
export const CONTACT_INTENT_STATUSES = [
  'unknown',
  'open',
  'conditional',
  'not_open',
  'withdrawn',
  'objected'
];
export const CONTACT_INTENT_CHANNELS = [
  'email',
  'whatsapp',
  'linkedin',
  'phone',
  'wechat',
  'other'
];
export const CONTACT_INTENT_PURPOSES = [
  'general_contact',
  'customer',
  'employment',
  'contractor',
  'project',
  'expert',
  'referral',
  'partnership'
];
export const EVIDENCE_REVIEW_REASON_CODES = [
  'confirmed_accurate',
  'insufficient_support',
  'incorrect',
  'outdated',
  'other'
];

const PERSON_STATUSES = new Set(['active', 'inactive', 'archived']);
const GOVERNANCE_STATUSES = new Set(['active', 'deletion_requested', 'anonymized']);

/** @param {unknown} value */
function plainObject(value) {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

/** @param {unknown} value @param {number} max */
function boundedText(value, max) {
  return typeof value === 'string' ? value.trim().slice(0, max) : '';
}

/** @param {unknown} value */
function safeInteger(value) {
  const number = Number(value);
  return Number.isFinite(number) ? Math.max(0, Math.trunc(number)) : 0;
}

/** @param {unknown} value */
function safeRatio(value) {
  const number = Number(value);
  return Number.isFinite(number) ? Math.min(1, Math.max(0, number)) : 0;
}

/** @param {unknown} value */
function safeDate(value) {
  if (typeof value !== 'string' || !value.trim()) return '';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? '' : parsed.toISOString();
}

/** @param {unknown} value */
export function isGovernanceUuid(value) {
  return typeof value === 'string' && UUID_RE.test(value);
}

/** @param {unknown} raw */
export function normalizeGovernanceSummary(raw) {
  const value = plainObject(raw) ? /** @type {Record<string, unknown>} */ (raw) : {};
  return {
    total: safeInteger(value.total),
    pendingAi: safeInteger(value.pending_ai),
    expiring: safeInteger(value.expiring),
    expired: safeInteger(value.expired),
    blocked: safeInteger(value.blocked),
    deletionRequested: safeInteger(value.deletion_requested),
    revision: safeInteger(value.revision)
  };
}

/** @param {unknown} raw */
function normalizePersonSummary(raw) {
  const value = plainObject(raw) ? /** @type {Record<string, unknown>} */ (raw) : {};
  const status = boundedText(value.status, 24).toLowerCase();
  return {
    id: isGovernanceUuid(value.id) ? String(value.id) : '',
    displayName: boundedText(value.display_name, 255) || 'Unnamed person',
    currentTitle: boundedText(value.current_title, 255),
    currentCompany: boundedText(value.current_company, 255),
    status: PERSON_STATUSES.has(status) ? status : ''
  };
}

/** @param {unknown} raw */
function normalizeEvidenceHealth(raw) {
  const value = plainObject(raw) ? /** @type {Record<string, unknown>} */ (raw) : {};
  const total = safeInteger(value.total);
  const pending = safeInteger(value.pending);
  const rejected = safeInteger(value.rejected);
  return {
    total,
    confirmed: safeInteger(value.confirmed),
    pending,
    restricted: safeInteger(value.restricted),
    expiring: safeInteger(value.expiring),
    expired: safeInteger(value.expired),
    rejected: safeInteger(value.rejected),
    lastObservedAt: safeDate(value.last_observed_at)
  };
}

/** @param {unknown} raw */
export function normalizeContactIntent(raw) {
  const value = plainObject(raw) ? /** @type {Record<string, unknown>} */ (raw) : {};
  const state = boundedText(value.state, 32).toLowerCase();
  const channel = boundedText(value.channel, 32).toLowerCase();
  const purpose = boundedText(value.purpose, 32).toLowerCase();
  return {
    id: isGovernanceUuid(value.id) ? String(value.id) : '',
    state: CONTACT_INTENT_STATUSES.includes(state) ? state : 'unknown',
    channel: CONTACT_INTENT_CHANNELS.includes(channel) ? channel : 'other',
    purpose: CONTACT_INTENT_PURPOSES.includes(purpose) ? purpose : 'general_contact',
    source: boundedText(value.source, 32).toLowerCase() === 'manual' ? 'manual' : '',
    confidence: safeRatio(value.confidence),
    observedAt: safeDate(value.observed_at),
    validUntil: safeDate(value.valid_until),
    revision: safeInteger(value.revision)
  };
}

/** @param {unknown} raw */
export function normalizeGovernancePerson(raw) {
  const value = plainObject(raw) ? /** @type {Record<string, unknown>} */ (raw) : {};
  const governanceStatus = boundedText(value.governance_status, 32).toLowerCase();
  const intents = Array.isArray(value.contact_intents)
    ? value.contact_intents
        .map(normalizeContactIntent)
        .filter((intent) => intent.id)
        .slice(0, 100)
    : [];
  const priority = ['objected', 'withdrawn', 'not_open', 'conditional', 'open', 'unknown'];
  const intent =
    [...intents].sort(
      (left, right) => priority.indexOf(left.state) - priority.indexOf(right.state)
    )[0] ?? normalizeContactIntent({});
  const blocked =
    governanceStatus !== 'active' || ['objected', 'withdrawn', 'not_open'].includes(intent.state);
  return {
    person: normalizePersonSummary(value),
    evidenceHealth: normalizeEvidenceHealth(value.evidence_summary),
    intents,
    intent,
    compliance: {
      state: blocked ? 'blocked' : 'unknown',
      blockedActions: blocked ? ['outreach', 'matching'] : [],
      blockedChannels: [],
      reasons: blocked
        ? [
            {
              code:
                governanceStatus !== 'active'
                  ? governanceStatus || 'governance_restricted'
                  : `intent_${intent.state}`,
              label:
                governanceStatus !== 'active'
                  ? 'Person governance status restricts processing'
                  : 'Recorded contact intent restricts outreach',
              severity: 'block'
            }
          ]
        : [],
      evaluatedAt: ''
    },
    retention: {
      status: GOVERNANCE_STATUSES.has(governanceStatus) ? governanceStatus : '',
      retentionUntil: safeDate(value.retention_until),
      deletionStatus:
        governanceStatus === 'anonymized'
          ? 'completed'
          : governanceStatus === 'deletion_requested'
            ? 'requested'
            : 'none',
      revision: safeInteger(value.governance_revision)
    },
    revision: safeInteger(value.governance_revision)
  };
}

/** @param {unknown} raw */
export function normalizeGovernanceEvidence(raw) {
  const value = plainObject(raw) ? /** @type {Record<string, unknown>} */ (raw) : {};
  const governance = plainObject(value.governance)
    ? /** @type {Record<string, unknown>} */ (value.governance)
    : {};
  const kind = boundedText(value.kind, 32).toLowerCase();
  const source = boundedText(value.source, 32).toLowerCase();
  const reviewStatus = boundedText(governance.confirmation_status, 32).toLowerCase();
  const freshness = boundedText(value.freshness, 32).toLowerCase();
  const validUntil = safeDate(value.valid_until);
  let derivedFreshness = EVIDENCE_FRESHNESS.includes(freshness) ? freshness : 'active';
  if (!EVIDENCE_FRESHNESS.includes(freshness) && validUntil) {
    const remaining = new Date(validUntil).getTime() - Date.now();
    derivedFreshness =
      remaining <= 0 ? 'expired' : remaining <= 30 * 86400000 ? 'expiring' : 'active';
  }
  return {
    id: isGovernanceUuid(value.id) ? String(value.id) : '',
    kind: EVIDENCE_KINDS.includes(kind) ? kind : 'other',
    source: EVIDENCE_SOURCES.includes(source) ? source : 'other',
    summary: boundedText(value.summary, 1000),
    observedAt: safeDate(value.observed_at),
    validUntil,
    confidence: safeRatio(value.confidence),
    reviewStatus: EVIDENCE_REVIEW_STATUSES.includes(reviewStatus) ? reviewStatus : 'pending',
    freshness: derivedFreshness,
    processingStatus: boundedText(governance.processing_status, 32).toLowerCase(),
    revision: safeInteger(governance.revision),
    aiGenerated: source === 'ai'
  };
}

/** @param {unknown} raw */
export function normalizeGovernanceList(raw) {
  const value = plainObject(raw) ? /** @type {Record<string, unknown>} */ (raw) : {};
  const results = Array.isArray(value.results)
    ? value.results
        .map(normalizeGovernancePerson)
        .filter((item) => item.person.id)
        .slice(0, 500)
    : [];
  return {
    count: safeInteger(value.count),
    summary: normalizeGovernanceSummary(value.summary),
    results
  };
}

/** @param {unknown} raw */
export function normalizeGovernanceDetail(raw) {
  const value = plainObject(raw) ? /** @type {Record<string, unknown>} */ (raw) : {};
  return {
    ...normalizeGovernancePerson(value),
    evidence: Array.isArray(value.evidence)
      ? value.evidence
          .map(normalizeGovernanceEvidence)
          .filter((item) => item.id)
          .slice(0, 500)
      : []
  };
}

/** @param {unknown} raw */
export function normalizeContactIntentList(raw) {
  const value = plainObject(raw) ? /** @type {Record<string, unknown>} */ (raw) : {};
  const items = Array.isArray(raw) ? raw : Array.isArray(value.results) ? value.results : [];
  return items
    .map(normalizeContactIntent)
    .filter((item) => item.id)
    .slice(0, 100);
}

/** @param {URLSearchParams} searchParams */
export function parseGovernanceFilters(searchParams) {
  const queue = boundedText(searchParams.get('queue'), 32).toLowerCase();
  const source = boundedText(searchParams.get('source'), 32).toLowerCase();
  const kind = boundedText(searchParams.get('kind'), 32).toLowerCase();
  const person = boundedText(searchParams.get('person'), 64);
  return {
    q: boundedText(searchParams.get('q'), 100),
    queue: GOVERNANCE_QUEUES.includes(queue) ? queue : '',
    source: EVIDENCE_SOURCES.includes(source) ? source : '',
    kind: EVIDENCE_KINDS.includes(kind) ? kind : '',
    person: isGovernanceUuid(person) ? person : ''
  };
}

/** @param {{ q?: string, queue?: string, source?: string, kind?: string }} filters */
export function buildGovernanceQuery(filters) {
  const params = new URLSearchParams({ limit: '100' });
  if (filters.q) params.set('q', filters.q);
  if (filters.queue) params.set('queue', filters.queue);
  if (filters.source) params.set('source', filters.source);
  if (filters.kind) params.set('kind', filters.kind);
  return params.toString();
}
