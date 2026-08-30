import { isUuid, normalizeMatch, OPPORTUNITY_TYPES } from './workbench.js';

export const FEEDBACK_WINDOWS = ['30d', '90d', '180d', 'all'];
export const FEEDBACK_QUEUES = ['pending_feedback', 'reviewed', 'has_outcome'];
export const ACCURACY_LABELS = ['accurate', 'partially_accurate', 'inaccurate', 'uncertain'];
export const OUTCOME_CODES = [
  'contact_attempted',
  'contact_reached',
  'interview_scheduled',
  'interview_completed',
  'deal_won',
  'deal_lost',
  'hired',
  'not_hired',
  'collaboration_started',
  'collaboration_completed',
  'referral_made',
  'referral_accepted',
  'not_pursued',
  'withdrew'
];
export const EVIDENCE_ASSESSMENTS = [
  'helpful',
  'neutral',
  'misleading',
  'outdated',
  'insufficient'
];
export const SCORING_DIMENSIONS = ['skills', 'titles', 'locations', 'availability'];
export const EVIDENCE_DIMENSIONS = [
  'skills',
  'titles',
  'locations',
  'availability',
  'trust',
  'relationship'
];
export const WEIGHT_VERSION_STATUSES = ['draft', 'published', 'rejected'];
export const SUGGESTION_STATUSES = ['pending', 'accepted', 'rejected'];
export const SUGGESTION_REVIEW_ACTIONS = ['accept', 'reject'];
export const REJECTION_REASON_CODES = [
  'criteria_gap',
  'availability_mismatch',
  'location_mismatch',
  'trust_risk',
  'insufficient_evidence',
  'contact_restricted',
  'opportunity_changed',
  'duplicate_or_engaged',
  'other'
];

const MATCH_STATUSES = new Set([
  'proposed',
  'reviewing',
  'shortlisted',
  'accepted',
  'rejected',
  'expired'
]);
const ACCURACY_PROJECTIONS = ['unknown', ...ACCURACY_LABELS];
const SAFE_CODE = /^[a-z0-9_.:-]{1,64}$/;

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
function safeCode(value) {
  const code = boundedText(value, 64).toLowerCase();
  return SAFE_CODE.test(code) ? code : '';
}

/** @param {unknown} raw */
function safeWeights(raw) {
  const value = plainObject(raw) ? /** @type {Record<string, unknown>} */ (raw) : {};
  return Object.fromEntries(
    SCORING_DIMENSIONS.map((dimension) => {
      const number = Number(value[dimension]);
      return [dimension, Number.isFinite(number) ? Math.min(100, Math.max(0, number)) : 0];
    })
  );
}

/** @param {unknown} raw @param {number} [max] */
function safeCodeList(raw, max = 20) {
  if (!Array.isArray(raw)) return [];
  return [...new Set(raw.map(safeCode).filter(Boolean))].slice(0, max);
}

/** @param {unknown} raw */
export function normalizeFeedbackOverview(raw) {
  const value = plainObject(raw) ? /** @type {Record<string, unknown>} */ (raw) : {};
  const coverage = plainObject(value.coverage)
    ? /** @type {Record<string, unknown>} */ (value.coverage)
    : {};
  const verdicts = plainObject(value.verdicts)
    ? /** @type {Record<string, unknown>} */ (value.verdicts)
    : {};
  const insights = plainObject(value.insights)
    ? /** @type {Record<string, unknown>} */ (value.insights)
    : {};
  const reviewed = safeInteger(coverage.reviewed_matches);
  const total = safeInteger(coverage.total_matches);
  const useful = safeInteger(verdicts.accurate) + safeInteger(verdicts.partially_accurate);
  const suppressed = insights.suppressed !== false;
  const insightRows = Array.isArray(insights.dimensions) ? insights.dimensions : [];
  /** @type {Map<string, {total:number,helpful:number,concern:number}>} */
  const byDimension = new Map();
  for (const rawRow of insightRows) {
    const row = plainObject(rawRow) ? /** @type {Record<string, unknown>} */ (rawRow) : {};
    const dimension = boundedText(row.dimension, 32).toLowerCase();
    const assessment = boundedText(row.assessment, 32).toLowerCase();
    if (!EVIDENCE_DIMENSIONS.includes(dimension) || !EVIDENCE_ASSESSMENTS.includes(assessment)) {
      continue;
    }
    const current = byDimension.get(dimension) || { total: 0, helpful: 0, concern: 0 };
    const count = safeInteger(row.count);
    current.total += count;
    if (assessment === 'helpful') current.helpful += count;
    if (assessment === 'misleading' || assessment === 'outdated') current.concern += count;
    byDimension.set(dimension, current);
  }
  return {
    summary: {
      total,
      feedbackDue: Math.max(0, total - reviewed),
      feedbackCaptured: reviewed,
      feedbackCoverage: safeRatio(coverage.rate),
      outcomeKnown: safeInteger(value.lifecycle_outcome_count),
      accuracySampleSize: reviewed,
      accuracyAgreement: reviewed > 0 ? useful / reviewed : 0,
      pendingSuggestions: safeInteger(value.pending_suggestions)
    },
    verdicts: Object.fromEntries(
      ACCURACY_PROJECTIONS.map((verdict) => [verdict, safeInteger(verdicts[verdict])])
    ),
    evidenceImpact: EVIDENCE_DIMENSIONS.map((dimension) => {
      const counts = byDimension.get(dimension) || { total: 0, helpful: 0, concern: 0 };
      return {
        dimension,
        sampleSize: counts.total,
        helpfulRate: counts.total > 0 ? counts.helpful / counts.total : 0,
        concernCount: counts.concern,
        suppressed
      };
    }),
    insightSampleSize: safeInteger(insights.sample_count),
    insightMinimumSample: safeInteger(insights.minimum_sample),
    insightsSuppressed: suppressed
  };
}

/** @param {unknown} raw */
export function normalizeFeedbackQueueItem(raw) {
  const value = plainObject(raw) ? /** @type {Record<string, unknown>} */ (raw) : {};
  const person = plainObject(value.person)
    ? /** @type {Record<string, unknown>} */ (value.person)
    : {};
  const opportunity = plainObject(value.opportunity)
    ? /** @type {Record<string, unknown>} */ (value.opportunity)
    : {};
  const latestOutcome = plainObject(value.latest_outcome)
    ? /** @type {Record<string, unknown>} */ (value.latest_outcome)
    : {};
  const match = normalizeMatch(value);
  const accuracy = boundedText(value.verdict ?? value.recommendation_verdict, 32).toLowerCase();
  const status = boundedText(value.status, 24).toLowerCase();
  const opportunityType = boundedText(opportunity.type ?? value.opportunity_type, 24).toLowerCase();
  const latestOutcomeCode = safeCode(latestOutcome.code ?? value.latest_outcome_code);
  const dueFlags = [];
  if (accuracy === 'unknown') dueFlags.push('needs_feedback');
  if (!latestOutcomeCode) dueFlags.push('needs_outcome');
  if (status === 'rejected') dueFlags.push('rejected');
  if (accuracy !== 'unknown' && latestOutcomeCode) dueFlags.push('complete');
  return {
    matchId: isUuid(value.match_id ?? value.id) ? String(value.match_id ?? value.id) : '',
    person: {
      id: isUuid(person.id ?? match.personSummary.id)
        ? String(person.id ?? match.personSummary.id)
        : '',
      displayName:
        boundedText(person.display_name ?? match.personSummary.displayName, 255) ||
        'Unnamed person',
      currentTitle: boundedText(person.current_title ?? match.personSummary.currentTitle, 255),
      currentCompany: boundedText(person.current_company ?? match.personSummary.currentCompany, 255)
    },
    opportunity: {
      id: isUuid(opportunity.id ?? value.opportunity)
        ? String(opportunity.id ?? value.opportunity)
        : '',
      title:
        boundedText(opportunity.title ?? value.opportunity_title, 255) || 'Untitled opportunity',
      type: OPPORTUNITY_TYPES.includes(opportunityType) ? opportunityType : ''
    },
    status: MATCH_STATUSES.has(status) ? status : 'proposed',
    overallScore: match.overallScore,
    accuracy: ACCURACY_PROJECTIONS.includes(accuracy) ? accuracy : 'unknown',
    feedbackRevision: safeInteger(value.feedback_revision),
    rankingRevision: safeInteger(value.ranking_revision),
    latestOutcomeCode: OUTCOME_CODES.includes(latestOutcomeCode) ? latestOutcomeCode : '',
    scoringPolicyVersionId: isUuid(value.scoring_policy_version)
      ? String(value.scoring_policy_version)
      : '',
    dueFlags,
    evaluatedAt: safeDate(value.evaluated_at),
    updatedAt: safeDate(value.updated_at)
  };
}

/** @param {unknown} raw */
function normalizeEvidenceAssessment(raw) {
  const value = plainObject(raw) ? /** @type {Record<string, unknown>} */ (raw) : {};
  const dimension = boundedText(value.dimension, 32).toLowerCase();
  const assessment = boundedText(value.assessment, 32).toLowerCase();
  return {
    evidenceId: isUuid(value.evidence_id ?? value.evidence)
      ? String(value.evidence_id ?? value.evidence)
      : '',
    dimension: EVIDENCE_DIMENSIONS.includes(dimension) ? dimension : '',
    assessment: EVIDENCE_ASSESSMENTS.includes(assessment) ? assessment : 'neutral'
  };
}

/** @param {unknown} raw */
function normalizeOutcome(raw) {
  const value = plainObject(raw) ? /** @type {Record<string, unknown>} */ (raw) : {};
  const outcomeCode = safeCode(value.outcome_code);
  return {
    id: isUuid(value.id) ? String(value.id) : '',
    outcomeCode: OUTCOME_CODES.includes(outcomeCode) ? outcomeCode : '',
    occurredAt: safeDate(value.occurred_at),
    revision: safeInteger(value.resulting_feedback_revision)
  };
}

/** @param {unknown} raw */
export function normalizeFeedbackDetail(raw) {
  const value = plainObject(raw) ? /** @type {Record<string, unknown>} */ (raw) : {};
  const normalizedMatch = normalizeMatch(value.match);
  const match = { ...normalizedMatch, decisionReason: '' };
  const events = Array.isArray(value.events)
    ? value.events
        .map((item) => {
          const event = plainObject(item) ? /** @type {Record<string, unknown>} */ (item) : {};
          const eventKind = safeCode(event.event_kind);
          const verdict = safeCode(event.verdict);
          const outcomeCode = safeCode(event.outcome_code);
          return {
            id: isUuid(event.id) ? String(event.id) : '',
            eventKind,
            verdict: ACCURACY_PROJECTIONS.includes(verdict) ? verdict : '',
            outcomeCode: OUTCOME_CODES.includes(outcomeCode) ? outcomeCode : '',
            reasonCode: safeCode(event.reason_code),
            occurredAt: safeDate(event.occurred_at),
            recordedAt: safeDate(event.recorded_at),
            revision: safeInteger(event.resulting_feedback_revision),
            evidenceAssessments: Array.isArray(event.attributions)
              ? event.attributions
                  .map(normalizeEvidenceAssessment)
                  .filter((entry) => entry.evidenceId && entry.dimension)
                  .slice(0, 20)
              : []
          };
        })
        .filter((event) => event.id)
        .slice(0, 100)
    : [];
  const currentFeedback = plainObject(value.current_feedback)
    ? /** @type {Record<string, unknown>} */ (value.current_feedback)
    : {};
  const latestFeedback = events.find((event) => event.eventKind === 'recommendation_feedback');
  const currentAccuracy = safeCode(currentFeedback.accuracy);
  const outcomes = Array.isArray(value.outcomes)
    ? value.outcomes
        .map(normalizeOutcome)
        .filter((item) => item.id)
        .slice(0, 100)
    : events
        .filter((event) => event.eventKind === 'lifecycle_outcome' && event.outcomeCode)
        .map((event) => ({
          id: event.id,
          outcomeCode: event.outcomeCode,
          occurredAt: event.occurredAt,
          revision: event.revision
        }));
  const outcomeOptions = Array.isArray(value.available_milestones)
    ? value.available_milestones
    : Array.isArray(value.allowed_outcomes)
      ? value.allowed_outcomes
      : [];
  const allowedOutcomes = outcomeOptions
    .map((item) => {
      const option = plainObject(item) ? /** @type {Record<string, unknown>} */ (item) : {};
      const code = safeCode(option.code ?? item);
      return {
        code: OUTCOME_CODES.includes(code) ? code : '',
        label: boundedText(option.label, 100) || code
      };
    })
    .filter((item) => item.code);
  return {
    match,
    feedback: {
      accuracy: ACCURACY_PROJECTIONS.includes(currentAccuracy)
        ? currentAccuracy
        : latestFeedback?.verdict || match.recommendationVerdict || 'unknown',
      rejectionReasonCode: latestFeedback?.reasonCode || '',
      revision: safeInteger(currentFeedback.revision ?? match.feedbackRevision),
      evidenceAssessments: Array.isArray(currentFeedback.evidence_assessments)
        ? currentFeedback.evidence_assessments
            .map(normalizeEvidenceAssessment)
            .filter((item) => item.evidenceId && item.dimension)
            .slice(0, 20)
        : latestFeedback?.evidenceAssessments || []
    },
    outcomes,
    feedbackRevision: safeInteger(currentFeedback.revision ?? match.feedbackRevision),
    availableOutcomes: allowedOutcomes,
    events
  };
}

/** @param {unknown} raw */
export function normalizeWeightVersion(raw) {
  const value = plainObject(raw) ? /** @type {Record<string, unknown>} */ (raw) : {};
  const status = boundedText(value.state, 24).toLowerCase();
  const type = boundedText(value.opportunity_type, 24).toLowerCase();
  return {
    id: isUuid(value.id) ? String(value.id) : '',
    policyId: isUuid(value.policy) ? String(value.policy) : '',
    opportunityType: OPPORTUNITY_TYPES.includes(type) ? type : '',
    version: safeInteger(value.version),
    status: WEIGHT_VERSION_STATUSES.includes(status) ? status : 'draft',
    weights: safeWeights(value.dimension_weights),
    source: ['human', 'ai_suggestion', 'legacy'].includes(String(value.source))
      ? String(value.source)
      : '',
    policyRevision: safeInteger(value.policy_revision),
    createdAt: safeDate(value.created_at),
    checksum: boundedText(value.checksum, 128)
  };
}

/** @param {unknown} raw */
export function normalizeWeightPolicyList(raw) {
  const value = plainObject(raw) ? /** @type {Record<string, unknown>} */ (raw) : {};
  const items = Array.isArray(raw) ? raw : Array.isArray(value.results) ? value.results : [];
  return items
    .flatMap((item) => {
      const policy = plainObject(item) ? /** @type {Record<string, unknown>} */ (item) : {};
      if (plainObject(policy.active_version_detail)) {
        return [
          normalizeWeightVersion({
            .../** @type {Record<string, unknown>} */ (policy.active_version_detail),
            opportunity_type: policy.opportunity_type,
            policy_revision: policy.revision
          })
        ];
      }
      return [normalizeWeightVersion(policy)];
    })
    .filter((item) => item.id)
    .slice(0, 200);
}

/** @param {unknown} raw */
export function normalizeCalibrationSuggestion(raw) {
  const value = plainObject(raw) ? /** @type {Record<string, unknown>} */ (raw) : {};
  const status = boundedText(value.status, 24).toLowerCase();
  const type = boundedText(value.opportunity_type, 24).toLowerCase();
  const currentWeights = safeWeights(value.current_weights);
  const proposedWeights = safeWeights(value.dimension_weights);
  const weightDeltas = Object.fromEntries(
    SCORING_DIMENSIONS.map((dimension) => [
      dimension,
      proposedWeights[dimension] - currentWeights[dimension]
    ])
  );
  return {
    id: isUuid(value.id) ? String(value.id) : '',
    opportunityType: OPPORTUNITY_TYPES.includes(type) ? type : '',
    status: SUGGESTION_STATUSES.includes(status) ? status : 'pending',
    revision: safeInteger(value.revision),
    policyId: isUuid(value.policy) ? String(value.policy) : '',
    currentWeightVersion: safeInteger(value.current_weight_version),
    currentWeights,
    proposedWeights,
    weightDeltas,
    rationaleCodes: value.rationale ? ['observational_feedback'] : [],
    warningCodes: ['non_causal', 'human_review_required'],
    sampleSize: safeInteger(value.sample_size),
    generator: safeCode(value.generator),
    acceptedDraftId: isUuid(value.accepted_draft) ? String(value.accepted_draft) : '',
    createdAt: safeDate(value.created_at),
    reviewedAt: safeDate(value.reviewed_at)
  };
}

/** @param {unknown} raw */
export function normalizeSuggestionList(raw) {
  const value = plainObject(raw) ? /** @type {Record<string, unknown>} */ (raw) : {};
  const items = Array.isArray(raw) ? raw : Array.isArray(value.results) ? value.results : [];
  return items
    .map(normalizeCalibrationSuggestion)
    .filter((item) => item.id)
    .slice(0, 200);
}

/** @param {URLSearchParams} searchParams */
export function parseFeedbackFilters(searchParams) {
  const type = boundedText(searchParams.get('type'), 24).toLowerCase();
  const window = boundedText(searchParams.get('window'), 8).toLowerCase();
  const queue = boundedText(searchParams.get('queue'), 32).toLowerCase();
  const match = boundedText(searchParams.get('match'), 64);
  const suggestion = boundedText(searchParams.get('suggestion'), 64);
  return {
    type: OPPORTUNITY_TYPES.includes(type) ? type : '',
    window: FEEDBACK_WINDOWS.includes(window) ? window : '90d',
    queue: FEEDBACK_QUEUES.includes(queue) ? queue : '',
    match: isUuid(match) ? match : '',
    suggestion: isUuid(suggestion) ? suggestion : ''
  };
}

/** @param {{type?:string,window?:string,queue?:string}} filters */
export function buildFeedbackQuery(filters) {
  const params = new URLSearchParams({ limit: '100' });
  if (filters.type) {
    params.set('type', filters.type);
    params.set('opportunity_type', filters.type);
  }
  if (filters.window && filters.window !== 'all') {
    params.set('window', filters.window.replace(/d$/, ''));
  }
  if (filters.queue) params.set('queue', filters.queue);
  return params.toString();
}

/** @param {unknown} raw */
export function parseEvidenceAssessments(raw) {
  if (typeof raw !== 'string' || raw.length > 20000) return [];
  try {
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed) || parsed.length > 20) return [];
    return parsed
      .map(normalizeEvidenceAssessment)
      .filter((item) => item.evidenceId && item.dimension)
      .map((item) => ({
        evidence_id: item.evidenceId,
        dimension: item.dimension,
        assessment: item.assessment
      }));
  } catch {
    return [];
  }
}

export { safeCode, safeDate, safeInteger };
