import test from 'node:test';
import assert from 'node:assert/strict';

import {
  buildGovernanceQuery,
  normalizeContactIntentList,
  normalizeGovernanceDetail,
  normalizeGovernanceList,
  parseGovernanceFilters
} from '../src/lib/matching/governance.js';

const PERSON_ID = '10000000-0000-4000-8000-000000000001';
const EVIDENCE_ID = '10000000-0000-4000-8000-000000000002';
const INTENT_ID = '10000000-0000-4000-8000-000000000003';

test('bounds governance URL filters and builds only supported API parameters', () => {
  const filters = parseGovernanceFilters(
    new URLSearchParams({
      q: `  Alice ${'x'.repeat(200)} `,
      queue: 'pending_ai',
      source: 'linkedin',
      kind: 'skill',
      person: PERSON_ID,
      raw_identity: 'private@example.com'
    })
  );
  assert.equal(filters.q.length, 100);
  assert.equal(filters.person, PERSON_ID);
  assert.equal(filters.queue, 'pending_ai');
  const query = new URLSearchParams(buildGovernanceQuery(filters));
  assert.equal(query.get('limit'), '100');
  assert.equal(query.get('q'), filters.q);
  assert.equal(query.get('queue'), 'pending_ai');
  assert.equal(query.get('source'), 'linkedin');
  assert.equal(query.get('kind'), 'skill');
  assert.equal(parseGovernanceFilters(new URLSearchParams({ queue: 'arbitrary' })).queue, '');
});

test('normalizes governance people without identities, legal notes or raw provider data', () => {
  const normalized = normalizeGovernanceList({
    count: 1,
    summary: {
      total: 1,
      pending_ai: 2,
      blocked: 1,
      revision: 4,
      legal_notes: 'private legal assessment'
    },
    results: [
      {
        id: PERSON_ID,
        display_name: 'Alice',
        current_title: 'Engineer',
        identity: 'private@example.com',
        governance_status: 'active',
        governance_revision: 2,
        evidence_summary: { total: 5, confirmed: 3, pending: 2, expiring: 1 },
        contact_intents: [
          {
            id: INTENT_ID,
            state: 'open',
            channel: 'email',
            purpose: 'employment',
            raw_text: 'private conversation'
          }
        ],
        provider_payload: { secret: true }
      }
    ]
  });

  assert.equal(normalized.results[0].person.displayName, 'Alice');
  assert.equal(normalized.results[0].intent.purpose, 'employment');
  assert.equal(normalized.results[0].intent.channel, 'email');
  assert.deepEqual(normalized.results[0].compliance.blockedActions, []);
  const serialized = JSON.stringify(normalized);
  assert.equal(serialized.includes('private@example.com'), false);
  assert.equal(serialized.includes('private legal assessment'), false);
  assert.equal(serialized.includes('private conversation'), false);
  assert.equal(serialized.includes('provider_payload'), false);
});

test('normalizes evidence detail through a safe allow-list', () => {
  const normalized = normalizeGovernanceDetail({
    id: PERSON_ID,
    display_name: 'Alice',
    governance_status: 'active',
    governance_revision: 2,
    evidence_summary: { total: 1, pending: 1 },
    contact_intents: [],
    evidence: [
      {
        id: EVIDENCE_ID,
        kind: 'skill',
        source: 'ai',
        summary: 'Likely experienced with Python.',
        observed_at: '2026-08-20T00:00:00Z',
        valid_until: '2026-10-01T00:00:00Z',
        confidence: 2,
        freshness: 'active',
        governance: {
          confirmation_status: 'pending',
          processing_status: 'active',
          revision: 5,
          lawful_basis_notes: 'private legal assessment'
        },
        facts: { skill: 'private' },
        source_uri: 'https://private.example',
        source_record_id: 'private-record',
        provider_error: 'private error'
      }
    ]
  });

  assert.equal(normalized.evidence[0].confidence, 1);
  assert.equal(normalized.evidence[0].reviewStatus, 'pending');
  assert.equal(normalized.evidence[0].aiGenerated, true);
  const serialized = JSON.stringify(normalized);
  assert.equal(serialized.includes('facts'), false);
  assert.equal(serialized.includes('private.example'), false);
  assert.equal(serialized.includes('private-record'), false);
  assert.equal(serialized.includes('private error'), false);
});

test('normalizes bounded contact intent history without raw notes', () => {
  const intents = normalizeContactIntentList({
    results: [
      {
        id: INTENT_ID,
        state: 'conditional',
        channel: 'linkedin',
        purpose: 'expert',
        source: 'manual',
        confidence: '0.750',
        observed_at: '2026-08-25T00:00:00Z',
        valid_until: '2026-12-01T00:00:00Z',
        revision: 3,
        reason: 'private conversation text'
      }
    ]
  });
  assert.equal(intents[0].state, 'conditional');
  assert.equal(intents[0].channel, 'linkedin');
  assert.equal(intents[0].purpose, 'expert');
  assert.equal(JSON.stringify(intents).includes('private conversation text'), false);
});
