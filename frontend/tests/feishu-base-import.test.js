import assert from 'node:assert/strict';
import test from 'node:test';

import {
  buildFeishuPersonImportExecution,
  buildFeishuPersonImportPreview,
  FEISHU_PERSON_IMPORT_ACTION,
  normalizeFeishuPersonImportConnection,
  normalizeFeishuPersonImportExecution,
  normalizeFeishuPersonImportIntent,
  validateFeishuPersonImportMapping
} from '../src/lib/feishu-base-import.js';

const APPROVAL_ID = '11111111-1111-4111-8111-111111111111';
const REQUEST_ID = '22222222-2222-4222-8222-222222222222';
const JOB_ID = '33333333-3333-4333-8333-333333333333';
const BATCH_ID = '44444444-4444-4444-8444-444444444444';
const CONNECTION_ID = '55555555-5555-4555-8555-555555555555';
const HASH_A = 'a'.repeat(64);
const HASH_B = 'b'.repeat(64);

test('one-time Feishu mapping accepts derived names and exact bounded payload', () => {
  const mapping = {
    first_name: 'Given name',
    email: 'Business email',
    observed_at: 'Last verified'
  };
  assert.deepEqual(validateFeishuPersonImportMapping(mapping), { mapping, error: '' });
  assert.deepEqual(buildFeishuPersonImportPreview(mapping, 100), { mapping, limit: 100 });
  assert.equal(buildFeishuPersonImportPreview(mapping, 501), null);
});

test('mapping rejects unsupported targets, duplicate provider fields, and missing identity or name', () => {
  assert.equal(
    validateFeishuPersonImportMapping({ display_name: 'Name', record_id: 'Remote id' }).mapping,
    null
  );
  assert.equal(
    validateFeishuPersonImportMapping({ display_name: 'Name', email: 'Name' }).mapping,
    null
  );
  assert.equal(validateFeishuPersonImportMapping({ display_name: 'Name' }).mapping, null);
  assert.equal(validateFeishuPersonImportMapping({ email: 'Email' }).mapping, null);
});

test('approval intent keeps only exact opaque safety values', () => {
  const normalized = normalizeFeishuPersonImportIntent({
    approval_required: true,
    intent: {
      channel: 'feishu',
      action: FEISHU_PERSON_IMPORT_ACTION,
      target_hash: HASH_A,
      payload_hash: HASH_B,
      test_target_identifier: `feishu-base:${CONNECTION_ID}`,
      units: 1,
      mapping: { email: 'Sensitive provider field' },
      app_token: 'secret',
      record_id: 'remote-record'
    },
    provider_body: { email: 'person@example.com' }
  });
  assert.deepEqual(normalized, {
    channel: 'feishu',
    action: FEISHU_PERSON_IMPORT_ACTION,
    targetHash: HASH_A,
    payloadHash: HASH_B,
    testTargetIdentifier: `feishu-base:${CONNECTION_ID}`,
    units: 1
  });
  assert.equal(JSON.stringify(normalized).includes('Sensitive'), false);
  assert.equal(JSON.stringify(normalized).includes('person@example.com'), false);
});

test('approved execution body contains only mapping, limit, approval, and stable scoped UUID', () => {
  const mapping = { display_name: 'Name', linkedin: 'LinkedIn profile' };
  assert.deepEqual(
    buildFeishuPersonImportExecution(mapping, 25, APPROVAL_ID, () => REQUEST_ID),
    {
      mapping,
      limit: 25,
      approval_id: APPROVAL_ID,
      idempotency_key: REQUEST_ID
    }
  );
  assert.equal(
    buildFeishuPersonImportExecution(mapping, 25, 'not-a-uuid', () => REQUEST_ID),
    null
  );
  assert.equal(
    buildFeishuPersonImportExecution(mapping, 25, APPROVAL_ID, () => APPROVAL_ID)?.idempotency_key,
    APPROVAL_ID
  );
});

test('async ledger projection strips URLs, hashes, provider response, PII, and remote ids', () => {
  const normalized = normalizeFeishuPersonImportExecution({
    id: REQUEST_ID,
    status: 'previewed',
    job_id: JOB_ID,
    batch_id: BATCH_ID,
    total_count: 12,
    ready_count: 10,
    invalid_count: 2,
    error_code: null,
    replayed: true,
    status_url: '/api/provider/unsafe',
    batch_url: `/matching/imports/${BATCH_ID}`,
    target_hash: HASH_A,
    mapping: { email: 'Private column' },
    remote_record_id: 'rec-secret',
    provider_response: { email: 'person@example.com' }
  });
  assert.deepEqual(normalized, {
    id: REQUEST_ID,
    status: 'previewed',
    jobId: JOB_ID,
    batchId: BATCH_ID,
    replayed: true,
    totalCount: 12,
    readyCount: 10,
    invalidCount: 2,
    errorCode: '',
    createdAt: '',
    completedAt: ''
  });
  const serialized = JSON.stringify(normalized);
  for (const forbidden of [
    'status_url',
    'target_hash',
    'Private column',
    'rec-secret',
    'person@'
  ]) {
    assert.equal(serialized.includes(forbidden), false);
  }
});

test('async ledger projection preserves the conservative unknown outcome', () => {
  assert.deepEqual(
    normalizeFeishuPersonImportExecution({
      id: REQUEST_ID,
      status: 'unknown',
      error_code: 'feishu_import_outcome_unknown'
    }),
    {
      id: REQUEST_ID,
      status: 'unknown',
      jobId: '',
      batchId: '',
      replayed: false,
      totalCount: 0,
      readyCount: 0,
      invalidCount: 0,
      errorCode: 'feishu_import_outcome_unknown',
      createdAt: '',
      completedAt: ''
    }
  );
});

test('connection projection exposes booleans only', () => {
  assert.deepEqual(
    normalizeFeishuPersonImportConnection({
      id: CONNECTION_ID,
      app_id_configured: true,
      app_secret_configured: true,
      app_token_configured: true,
      table_id_configured: true,
      is_active: true,
      app_token: 'secret',
      table_id: 'table',
      field_mapping: { email: 'Private column' }
    }),
    { configured: true, active: true }
  );
  assert.deepEqual(
    normalizeFeishuPersonImportConnection({
      id: CONNECTION_ID,
      app_secret_configured: true,
      is_active: true
    }),
    { configured: false, active: true }
  );
});
