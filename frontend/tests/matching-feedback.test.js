import test from 'node:test';
import assert from 'node:assert/strict';

import {
  buildFeedbackQuery,
  normalizeCalibrationSuggestion,
  normalizeFeedbackDetail,
  normalizeFeedbackOverview,
  normalizeFeedbackQueueItem,
  normalizeWeightVersion,
  parseEvidenceAssessments,
  parseFeedbackFilters
} from '../src/lib/matching/feedback.js';

const MATCH_ID = '10000000-0000-4000-8000-000000000001';
const PERSON_ID = '10000000-0000-4000-8000-000000000002';
const OPPORTUNITY_ID = '10000000-0000-4000-8000-000000000003';
const EVIDENCE_ID = '10000000-0000-4000-8000-000000000004';
const VERSION_ID = '10000000-0000-4000-8000-000000000005';
const SUGGESTION_ID = '10000000-0000-4000-8000-000000000006';

test('bounds feedback URL state and only forwards supported filters', () => {
  const filters = parseFeedbackFilters(
    new URLSearchParams({
      type: 'employment',
      window: '90d',
      queue: 'pending_feedback',
      match: MATCH_ID,
      suggestion: SUGGESTION_ID,
      identity: 'private@example.com'
    })
  );
  assert.equal(filters.match, MATCH_ID);
  assert.equal(filters.suggestion, SUGGESTION_ID);
  const query = new URLSearchParams(buildFeedbackQuery(filters));
  assert.equal(query.get('type'), 'employment');
  assert.equal(query.get('opportunity_type'), 'employment');
  assert.equal(query.get('window'), '90');
  assert.equal(query.get('queue'), 'pending_feedback');
  assert.equal(query.has('identity'), false);
  assert.equal(parseFeedbackFilters(new URLSearchParams({ window: 'forever' })).window, '90d');
});

test('normalizes aggregate feedback and preserves suppression without raw examples', () => {
  const normalized = normalizeFeedbackOverview({
    coverage: {
      total_matches: 20,
      reviewed_matches: 15,
      rate: 0.75
    },
    lifecycle_outcome_count: 10,
    pending_suggestions: 2,
    verdicts: { accurate: 8, partially_accurate: 4, inaccurate: 2, uncertain: 1 },
    insights: {
      suppressed: false,
      minimum_sample: 10,
      sample_count: 15,
      dimensions: [
        { dimension: 'skills', assessment: 'helpful', count: 3, person_ids: [PERSON_ID] },
        { dimension: 'skills', assessment: 'outdated', count: 1 }
      ]
    },
    provider_payload: { secret: true }
  });
  assert.equal(normalized.summary.feedbackCoverage, 0.75);
  assert.equal(normalized.summary.accuracyAgreement, 0.8);
  assert.equal(normalized.evidenceImpact[0].suppressed, false);
  assert.equal(normalized.evidenceImpact[0].concernCount, 1);
  const serialized = JSON.stringify(normalized);
  assert.equal(serialized.includes('private'), false);
  assert.equal(serialized.includes(PERSON_ID), false);
  assert.equal(serialized.includes('provider_payload'), false);
});

test('normalizes queue items without identities or free-form notes', () => {
  const item = normalizeFeedbackQueueItem({
    match_id: MATCH_ID,
    person: { id: PERSON_ID, display_name: 'Alice', current_title: 'Engineer' },
    opportunity: { id: OPPORTUNITY_ID, title: 'Platform role', type: 'employment' },
    status: 'rejected',
    overall_score: 81,
    verdict: 'inaccurate',
    latest_outcome: { code: '', occurred_at: null },
    feedback_revision: 2,
    ranking_revision: 3,
    identity: 'private@example.com',
    note: 'private conversation'
  });
  assert.equal(item.matchId, MATCH_ID);
  assert.equal(item.person.displayName, 'Alice');
  assert.deepEqual(item.dueFlags, ['needs_outcome', 'rejected']);
  assert.equal(JSON.stringify(item).includes('private'), false);
});

test('normalizes feedback detail through existing safe match projection', () => {
  const detail = normalizeFeedbackDetail({
    match: {
      id: MATCH_ID,
      person: PERSON_ID,
      person_name: 'Alice',
      status: 'shortlisted',
      overall_score: 88,
      ranking_revision: 4,
      feedback_revision: 3,
      recommendation_verdict: 'accurate',
      evidence_links: [
        {
          id: '10000000-0000-4000-8000-000000000007',
          evidence: {
            id: EVIDENCE_ID,
            kind: 'skill',
            source: 'ai',
            summary: 'Bounded summary',
            facts: { raw: 'secret' },
            source_uri: 'https://private.example'
          }
        }
      ]
    },
    allowed_outcomes: ['interview_scheduled', 'hired', 'private_outcome'],
    events: [
      {
        id: '10000000-0000-4000-8000-000000000008',
        event_kind: 'recommendation_feedback',
        verdict: 'accurate',
        reason_code: 'assessment_recorded',
        resulting_feedback_revision: 3,
        attributions: [{ evidence: EVIDENCE_ID, dimension: 'skills', assessment: 'helpful' }],
        raw_note: 'private'
      }
    ]
  });
  assert.equal(detail.match.id, MATCH_ID);
  assert.equal(detail.feedback.evidenceAssessments[0].assessment, 'helpful');
  assert.equal(detail.availableOutcomes[0].code, 'interview_scheduled');
  const serialized = JSON.stringify(detail);
  assert.equal(serialized.includes('secret'), false);
  assert.equal(serialized.includes('private.example'), false);
  assert.equal(serialized.includes('raw_note'), false);
});

test('parses only bounded evidence assessment JSON', () => {
  const parsed = parseEvidenceAssessments(
    JSON.stringify([
      { evidence_id: EVIDENCE_ID, dimension: 'skills', assessment: 'misleading' },
      { evidence_id: PERSON_ID, dimension: 'private_dimension', assessment: 'helpful' }
    ])
  );
  assert.deepEqual(parsed, [
    { evidence_id: EVIDENCE_ID, dimension: 'skills', assessment: 'misleading' }
  ]);
  assert.deepEqual(parseEvidenceAssessments('{invalid'), []);
});

test('normalizes immutable weight versions to four supported dimensions', () => {
  const version = normalizeWeightVersion({
    id: VERSION_ID,
    opportunity_type: 'employment',
    version: 3,
    state: 'published',
    source: 'human',
    dimension_weights: {
      skills: 40,
      titles: 20,
      locations: 10,
      availability: 30,
      private: 99
    },
    policy_revision: 2,
    prompt: 'private'
  });
  assert.deepEqual(version.weights, {
    skills: 40,
    titles: 20,
    locations: 10,
    availability: 30
  });
  assert.equal(JSON.stringify(version).includes('prompt'), false);
});

test('normalizes AI suggestions without prompts, completions or provider errors', () => {
  const suggestion = normalizeCalibrationSuggestion({
    id: SUGGESTION_ID,
    opportunity_type: 'employment',
    status: 'pending',
    revision: 4,
    current_weight_version: 2,
    current_weights: { skills: 45, titles: 20, locations: 15, availability: 20 },
    dimension_weights: { skills: 40, titles: 25, locations: 15, availability: 20 },
    rationale: 'Observational analytics draft; human review required.',
    generator: 'observational-v1',
    sample_size: 20,
    prompt: 'private prompt',
    completion: 'private completion',
    provider_error: 'private error'
  });
  assert.equal(suggestion.id, SUGGESTION_ID);
  assert.deepEqual(suggestion.rationaleCodes, ['observational_feedback']);
  assert.equal(suggestion.weightDeltas.skills, -5);
  const serialized = JSON.stringify(suggestion);
  assert.equal(serialized.includes('private'), false);
});
