import test from 'node:test';
import assert from 'node:assert/strict';

import {
  buildCreateOpportunityPayload,
  buildOpportunityQuery,
  chooseOpportunity,
  decisionTargetsForStatus,
  filterOpportunities,
  isDecisionStatus,
  isMatchRunActive,
  isMatchRunSkipped,
  isMatchRunSuccessful,
  isMatchRunTerminal,
  normalizeMatch,
  normalizeMatchRun,
  normalizeMatchingCapabilities,
  parseWorkbenchFilters,
  scoreLabel
} from '../src/lib/matching/workbench.js';

const OPEN_ID = '10000000-0000-4000-8000-000000000001';
const DRAFT_ID = '10000000-0000-4000-8000-000000000002';
const RUN_ID = '10000000-0000-4000-8000-000000000003';

test('normalizes matching capabilities with strict fail-closed booleans', () => {
  assert.deepEqual(
    normalizeMatchingCapabilities({
      read: true,
      manage: false,
      recompute: 'true',
      decide: 1,
      payload: { admin: true }
    }),
    { read: true, manage: false, recompute: false, decide: false }
  );
  assert.deepEqual(normalizeMatchingCapabilities(null), {
    read: false,
    manage: false,
    recompute: false,
    decide: false
  });
  assert.deepEqual(normalizeMatchingCapabilities(['read', 'decide']), {
    read: false,
    manage: false,
    recompute: false,
    decide: false
  });
});

test('parses only bounded URL filter values and valid UUID selections', () => {
  const parsed = parseWorkbenchFilters(
    new URLSearchParams({
      q: `  engineer ${'x'.repeat(120)}  `,
      status: 'OPEN',
      type: 'employment',
      match_status: 'shortlisted',
      opportunity: OPEN_ID,
      run: RUN_ID
    })
  );

  assert.equal(parsed.status, 'open');
  assert.equal(parsed.type, 'employment');
  assert.equal(parsed.matchStatus, 'shortlisted');
  assert.equal(parsed.opportunity, OPEN_ID);
  assert.equal(parsed.run, RUN_ID);
  assert.equal(parsed.q.length, 100);

  const rejected = parseWorkbenchFilters(
    new URLSearchParams({ status: 'deleted', type: 'unknown', opportunity: '../admin' })
  );
  assert.deepEqual(rejected, {
    q: '',
    status: '',
    type: '',
    matchStatus: '',
    opportunity: '',
    run: ''
  });
});

test('builds only supported server-side opportunity filters', () => {
  assert.equal(
    buildOpportunityQuery({ status: 'open', type: 'employment', q: 'not-sent' }, 999),
    'limit=500&status=open&type=employment'
  );
});

test('builds a bounded structured opportunity payload from human-readable criteria', () => {
  const form = new FormData();
  form.set('title', '  AI growth engineer  ');
  form.set('opportunity_type', 'employment');
  form.set('status', 'open');
  form.set('description', 'Build evidence-backed outbound systems.');
  form.set('organization_name', 'BottleCRM');
  form.set('location', 'Shanghai');
  form.set('remote_mode', 'hybrid');
  form.set('required_skills', 'Python, Django\npython');
  form.set('preferred_skills', 'PostgreSQL; CRM operations');
  form.set('required_titles', 'Growth engineer');
  form.set('required_locations', 'Shanghai, Remote');

  assert.deepEqual(buildCreateOpportunityPayload(form), {
    payload: {
      opportunity_type: 'employment',
      status: 'open',
      title: 'AI growth engineer',
      description: 'Build evidence-backed outbound systems.',
      organization_name: 'BottleCRM',
      location: 'Shanghai',
      remote_mode: 'hybrid',
      required_criteria: {
        skills: ['Python', 'Django'],
        titles: ['Growth engineer'],
        locations: ['Shanghai', 'Remote']
      },
      preferred_criteria: { skills: ['PostgreSQL', 'CRM operations'] },
      exclusion_criteria: {}
    }
  });

  form.set('opportunity_type', 'administrator');
  assert.deepEqual(buildCreateOpportunityPayload(form), {
    error: 'Select a valid opportunity type.'
  });

  form.set('opportunity_type', 'employment');
  form.set('required_skills', `${'x'.repeat(121)}, Python`);
  assert.deepEqual(buildCreateOpportunityPayload(form), {
    error: 'Each criterion must be at most 120 characters.'
  });
});

test('filters opportunities locally and chooses requested, open, then first', () => {
  const opportunities = [
    {
      id: DRAFT_ID,
      title: 'Design contractor',
      organizationName: 'Acme',
      location: 'London',
      type: 'contractor',
      status: 'draft'
    },
    {
      id: OPEN_ID,
      title: 'AI SDR Engineer',
      organizationName: 'Bottle',
      location: 'Shanghai',
      type: 'employment',
      status: 'open'
    }
  ];

  assert.deepEqual(filterOpportunities(opportunities, 'shanghai'), [opportunities[1]]);
  assert.equal(chooseOpportunity(opportunities, DRAFT_ID), opportunities[0]);
  assert.equal(chooseOpportunity(opportunities, 'missing'), opportunities[1]);
  assert.equal(chooseOpportunity([], ''), null);
});

test('normalizes matches without exposing raw evidence or identity fields', () => {
  const normalized = normalizeMatch({
    id: OPEN_ID,
    person: DRAFT_ID,
    person_name: 'Alice',
    person_summary: {
      id: DRAFT_ID,
      display_name: 'Alice Zhang',
      current_title: 'Growth Engineer',
      current_company: 'Bottle',
      location: 'Shanghai',
      availability: 'open_to_offers'
    },
    status: 'shortlisted',
    overall_score: 91,
    ranking_revision: 4,
    decision_revision: 2,
    decision_reason: 'Strong evidence and confirmed interest.',
    decided_at: '2026-08-25T01:00:00Z',
    evidence_links: [
      {
        id: 'link',
        explanation: 'skills: python',
        relevance: '0.8',
        contribution: '12.5',
        evidence: {
          id: 'evidence',
          kind: 'skill',
          source: 'linkedin',
          summary: 'Delivered a Python project.',
          facts: { skills: ['python'] },
          source_uri: 'https://private.example/profile',
          source_record_id: 'private-record',
          identities: [{ normalized_value: 'private@example.com' }],
          observed_at: '2026-08-24T00:00:00Z',
          confidence: '0.9'
        }
      }
    ]
  });

  assert.equal(normalized.overallScore, 91);
  assert.deepEqual(normalized.personSummary, {
    id: DRAFT_ID,
    displayName: 'Alice Zhang',
    currentTitle: 'Growth Engineer',
    currentCompany: 'Bottle',
    location: 'Shanghai',
    availability: 'open_to_offers'
  });
  assert.equal(normalized.personName, 'Alice Zhang');
  assert.equal(normalized.rankingRevision, 4);
  assert.equal(normalized.decisionRevision, 2);
  assert.equal(normalized.decisionReason, 'Strong evidence and confirmed interest.');
  assert.equal(normalized.evidenceLinks[0].evidence.summary, 'Delivered a Python project.');
  const serialized = JSON.stringify(normalized);
  assert.equal(serialized.includes('source_uri'), false);
  assert.equal(serialized.includes('private.example'), false);
  assert.equal(serialized.includes('facts'), false);
  assert.equal(serialized.includes('private@example.com'), false);
  assert.equal(serialized.includes('source_record_id'), false);
});

test('normalizes safe match-run progress without exposing automation internals', () => {
  const normalized = normalizeMatchRun({
    id: RUN_ID,
    opportunity: OPEN_ID,
    status: 'running',
    outcome: '',
    total_count: 40,
    processed_count: 55,
    result_count: null,
    ranking_revision: null,
    engine_version: 'rules-v2',
    started_at: '2026-08-25T00:00:00Z',
    error_code: 'WORKER_TIMEOUT',
    payload: { person_ids: ['private-person'] },
    result: { raw_matches: ['private-match'] },
    last_error_message: 'raw worker traceback',
    created_at: '2026-08-25T00:00:00Z',
    updated_at: '2026-08-25T00:01:00Z'
  });

  assert.equal(normalized.processedCount, 40);
  assert.equal(normalized.progress, 100);
  assert.equal(normalized.errorCode, 'WORKER_TIMEOUT');
  assert.equal(isMatchRunActive(normalized), true);
  assert.equal(isMatchRunTerminal(normalized), false);
  const serialized = JSON.stringify(normalized);
  assert.equal(serialized.includes('person_ids'), false);
  assert.equal(serialized.includes('private-person'), false);
  assert.equal(serialized.includes('private-match'), false);
  assert.equal(serialized.includes('raw worker traceback'), false);
  assert.equal(serialized.includes('payload'), false);
});

test('classifies terminal runs from status or outcome', () => {
  const succeeded = normalizeMatchRun({
    id: RUN_ID,
    opportunity: OPEN_ID,
    status: 'succeeded',
    outcome: 'succeeded'
  });
  assert.equal(isMatchRunActive(succeeded), false);
  assert.equal(isMatchRunTerminal(succeeded), true);
  assert.equal(isMatchRunSuccessful(succeeded), true);

  const failed = normalizeMatchRun({
    id: RUN_ID,
    opportunity: OPEN_ID,
    status: 'dead_letter',
    outcome: 'failed'
  });
  assert.equal(isMatchRunTerminal(failed), true);
  assert.equal(isMatchRunSuccessful(failed), false);

  const skipped = normalizeMatchRun({
    id: RUN_ID,
    opportunity: OPEN_ID,
    status: 'succeeded',
    outcome: 'skipped'
  });
  assert.equal(isMatchRunTerminal(skipped), true);
  assert.equal(isMatchRunSkipped(skipped), true);
  assert.equal(isMatchRunSuccessful(skipped), false);
});

test('limits human decision targets to the backend transition matrix', () => {
  assert.deepEqual(decisionTargetsForStatus('proposed'), ['reviewing', 'shortlisted', 'rejected']);
  assert.deepEqual(decisionTargetsForStatus('reviewing'), ['shortlisted', 'rejected']);
  assert.deepEqual(decisionTargetsForStatus('shortlisted'), ['reviewing', 'accepted', 'rejected']);
  assert.deepEqual(decisionTargetsForStatus('accepted'), []);
  assert.deepEqual(decisionTargetsForStatus('rejected'), []);
  assert.deepEqual(decisionTargetsForStatus('expired'), []);
});

test('limits human decisions and labels score boundaries', () => {
  assert.equal(isDecisionStatus('accepted'), true);
  assert.equal(isDecisionStatus('expired'), false);
  assert.equal(isDecisionStatus('admin'), false);
  assert.equal(scoreLabel(100), 'Strong fit');
  assert.equal(scoreLabel(79), 'Good fit');
  assert.equal(scoreLabel(59), 'Possible fit');
  assert.equal(scoreLabel(49), 'Weak fit');
});
