import assert from 'node:assert/strict';
import test from 'node:test';

import {
  buildApolloApprovedExecution,
  normalizeApolloApprovalRequired,
  normalizeApolloCandidateResponse,
  normalizeApolloIntent
} from '../src/lib/apollo-execution.js';

const SOURCE_ID = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const CANDIDATE_ID = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
const APPROVAL_ID = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc';
const IDEMPOTENCY_KEY = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd';
const HASH = 'a'.repeat(64);

test('approval-required projection keeps only exact non-PII intent fields', () => {
  const intent = normalizeApolloApprovalRequired(
    {
      status: 'approval_required',
      intent: {
        channel: 'apollo',
        action: 'search_people',
        payload_hash: HASH,
        target_hash: 'private-target-hash',
        test_target_identifier: `outbound-source:${SOURCE_ID}`,
        units: 1,
        provider_person_id: 'provider-secret',
        email: 'private@example.com'
      }
    },
    'search_people',
    `outbound-source:${SOURCE_ID}`
  );

  assert.deepEqual(intent, {
    action: 'search_people',
    payloadHash: HASH,
    testTargetIdentifier: `outbound-source:${SOURCE_ID}`,
    units: 1
  });
  const serialized = JSON.stringify(intent);
  assert.equal(serialized.includes('provider-secret'), false);
  assert.equal(serialized.includes('private@example.com'), false);
  assert.equal(serialized.includes('private-target-hash'), false);
});

test('intent projection rejects action, target, hash, and channel mismatches', () => {
  const base = {
    channel: 'apollo',
    action: 'enrich_person',
    payload_hash: HASH,
    test_target_identifier: `apollo-candidate:${CANDIDATE_ID}`,
    units: 1
  };
  assert.ok(normalizeApolloIntent(base, 'enrich_person', `apollo-candidate:${CANDIDATE_ID}`));
  assert.equal(
    normalizeApolloIntent(
      { ...base, channel: 'email' },
      'enrich_person',
      base.test_target_identifier
    ),
    null
  );
  assert.equal(
    normalizeApolloIntent(
      { ...base, action: 'search_people' },
      'enrich_person',
      base.test_target_identifier
    ),
    null
  );
  assert.equal(
    normalizeApolloIntent(
      { ...base, payload_hash: 'bad' },
      'enrich_person',
      base.test_target_identifier
    ),
    null
  );
  assert.equal(normalizeApolloIntent(base, 'enrich_person', `apollo-candidate:${SOURCE_ID}`), null);
});

test('approved execution accepts only an approval UUID and creates a fresh server key', () => {
  assert.deepEqual(
    buildApolloApprovedExecution(APPROVAL_ID, () => IDEMPOTENCY_KEY),
    {
      approval_id: APPROVAL_ID,
      idempotency_key: IDEMPOTENCY_KEY
    }
  );
  assert.equal(
    buildApolloApprovedExecution('not-a-uuid', () => IDEMPOTENCY_KEY),
    null
  );
  assert.throws(
    () => buildApolloApprovedExecution(APPROVAL_ID, () => 'browser-controlled-key'),
    /server could not generate/i
  );
});

test('candidate projection binds source and strips provider identity and PII', () => {
  const result = normalizeApolloCandidateResponse(
    {
      count: 999,
      results: [
        {
          id: CANDIDATE_ID,
          source_id: SOURCE_ID,
          safe_label: 'Apollo candidate ab12cd34',
          status: 'pending_enrichment_approval',
          provider_person_id: 'provider-secret',
          provider_person_id_hash: 'provider-hash',
          email: 'private@example.com',
          raw_payload: { private: true }
        },
        {
          id: 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee',
          source_id: 'ffffffff-ffff-4fff-8fff-ffffffffffff',
          safe_label: 'Wrong source',
          status: 'pending_enrichment_approval'
        },
        {
          id: '11111111-1111-4111-8111-111111111111',
          source_id: SOURCE_ID,
          safe_label: 'Apollo candidate needs review',
          status: 'import_review_required'
        }
      ]
    },
    SOURCE_ID
  );

  assert.deepEqual(result, {
    count: 2,
    results: [
      {
        id: CANDIDATE_ID,
        safeLabel: 'Apollo candidate ab12cd34',
        status: 'pending_enrichment_approval'
      },
      {
        id: '11111111-1111-4111-8111-111111111111',
        safeLabel: 'Apollo candidate needs review',
        status: 'import_review_required'
      }
    ]
  });
  const serialized = JSON.stringify(result);
  assert.equal(serialized.includes('provider-secret'), false);
  assert.equal(serialized.includes('provider-hash'), false);
  assert.equal(serialized.includes('private@example.com'), false);
  assert.equal(serialized.includes('raw_payload'), false);
});
