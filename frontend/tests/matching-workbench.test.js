import test from 'node:test';
import assert from 'node:assert/strict';

import {
  buildOpportunityQuery,
  chooseOpportunity,
  filterOpportunities,
  isDecisionStatus,
  normalizeMatch,
  parseWorkbenchFilters,
  scoreLabel
} from '../src/lib/matching/workbench.js';

const OPEN_ID = '10000000-0000-4000-8000-000000000001';
const DRAFT_ID = '10000000-0000-4000-8000-000000000002';

test('parses only bounded URL filter values and valid UUID selections', () => {
  const parsed = parseWorkbenchFilters(
    new URLSearchParams({
      q: `  engineer ${'x'.repeat(120)}  `,
      status: 'OPEN',
      type: 'employment',
      match_status: 'shortlisted',
      opportunity: OPEN_ID
    })
  );

  assert.equal(parsed.status, 'open');
  assert.equal(parsed.type, 'employment');
  assert.equal(parsed.matchStatus, 'shortlisted');
  assert.equal(parsed.opportunity, OPEN_ID);
  assert.equal(parsed.q.length, 100);

  const rejected = parseWorkbenchFilters(
    new URLSearchParams({ status: 'deleted', type: 'unknown', opportunity: '../admin' })
  );
  assert.deepEqual(rejected, {
    q: '',
    status: '',
    type: '',
    matchStatus: '',
    opportunity: ''
  });
});

test('builds only supported server-side opportunity filters', () => {
  assert.equal(
    buildOpportunityQuery({ status: 'open', type: 'employment', q: 'not-sent' }, 999),
    'limit=500&status=open&type=employment'
  );
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
  assert.equal(normalized.evidenceLinks[0].evidence.summary, 'Delivered a Python project.');
  const serialized = JSON.stringify(normalized);
  assert.equal(serialized.includes('source_uri'), false);
  assert.equal(serialized.includes('private.example'), false);
  assert.equal(serialized.includes('facts'), false);
  assert.equal(serialized.includes('private@example.com'), false);
  assert.equal(serialized.includes('source_record_id'), false);
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
