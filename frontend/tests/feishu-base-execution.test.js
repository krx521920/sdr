import assert from 'node:assert/strict';
import test from 'node:test';
import {
  FEISHU_BASE_ACTIONS,
  buildFeishuBaseApprovedExecution,
  buildFeishuBaseConnectionWrite,
  normalizeFeishuBaseConnection,
  normalizeFeishuBaseIntent,
  normalizeFeishuBaseSyncs,
  normalizeFeishuSchemaResult
} from '../src/lib/feishu-base-execution.js';

const APPROVAL_ID = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const EXECUTION_ID = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
const INTAKE_ID = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc';
const SYNC_ID = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd';

test('connection writes preserve blank credentials and can explicitly enable config only', () => {
  assert.deepEqual(
    buildFeishuBaseConnectionWrite({
      appId: '',
      appSecret: '',
      appToken: 'replacement-token',
      tableId: 'replacement-table',
      fieldMapping: { intake_id: 'Intake ID', unknown: 'Discard me' },
      isActive: true,
      execute: true
    }),
    {
      is_active: true,
      app_token: 'replacement-token',
      table_id: 'replacement-table',
      field_mapping: { intake_id: 'Intake ID' }
    }
  );
});

test('connection projection discards Feishu provider identifiers and secrets', () => {
  const result = normalizeFeishuBaseConnection({
    id: APPROVAL_ID,
    app_id: 'cli_private',
    app_token: 'bascn_private',
    table_id: 'tbl_private',
    app_secret_hint: 'secret-hint',
    app_secret_configured: true,
    is_active: true,
    field_mapping: { email: 'Private email field' },
    sync_summary: { total: 7, succeeded: 3, failed: -1 }
  });
  assert.deepEqual(result, {
    id: APPROVAL_ID,
    configured: true,
    secretConfigured: true,
    appIdConfigured: true,
    targetConfigured: true,
    active: true,
    fieldMapping: { email: 'Private email field' },
    lastValidatedAt: '',
    lastSyncAt: '',
    syncSummary: {
      total: 7,
      pending: 0,
      queued: 0,
      syncing: 0,
      succeeded: 3,
      failed: 0,
      skipped: 0,
      unknown: 0,
      external_erasure_pending: 0,
      external_erasure_completed: 0
    }
  });
  const serialized = JSON.stringify(result);
  assert.equal(serialized.includes('cli_private'), false);
  assert.equal(serialized.includes('bascn_private'), false);
  assert.equal(serialized.includes('tbl_private'), false);
  assert.equal(serialized.includes('Private email field'), true);
});

test('intent normalization requires exact Feishu action and safe fields', () => {
  const response = {
    approval_required: true,
    intent: {
      channel: 'feishu',
      action: FEISHU_BASE_ACTIONS.syncResearch,
      payload_hash: 'a'.repeat(64),
      target_hash: 'must-not-leak',
      test_target_identifier: `feishu-base:${APPROVAL_ID}`,
      units: 1,
      app_token: 'private',
      table_id: 'private'
    }
  };
  const normalized = normalizeFeishuBaseIntent(response, FEISHU_BASE_ACTIONS.syncResearch);
  assert.deepEqual(normalized, {
    channel: 'feishu',
    action: FEISHU_BASE_ACTIONS.syncResearch,
    payloadHash: 'a'.repeat(64),
    testTargetIdentifier: `feishu-base:${APPROVAL_ID}`,
    units: 1
  });
  assert.equal(JSON.stringify(normalized).includes('must-not-leak'), false);
  assert.equal(normalizeFeishuBaseIntent(response, FEISHU_BASE_ACTIONS.deleteResearch), null);
  assert.equal(
    normalizeFeishuBaseIntent(
      { ...response, intent: { ...response.intent, payload_hash: 'bad' } },
      FEISHU_BASE_ACTIONS.syncResearch
    ),
    null
  );
  assert.equal(
    normalizeFeishuBaseIntent(
      {
        ...response,
        intent: { ...response.intent, test_target_identifier: `feishu-intake:${INTAKE_ID}` }
      },
      FEISHU_BASE_ACTIONS.syncResearch
    ),
    null
  );
});

test('approved execution accepts only approval UUID and generates server idempotency UUID', () => {
  assert.deepEqual(
    buildFeishuBaseApprovedExecution(APPROVAL_ID, () => EXECUTION_ID),
    {
      approval_id: APPROVAL_ID,
      idempotency_key: EXECUTION_ID
    }
  );
  assert.equal(
    buildFeishuBaseApprovedExecution('bad', () => EXECUTION_ID),
    null
  );
  assert.equal(
    buildFeishuBaseApprovedExecution(APPROVAL_ID, () => 'bad'),
    null
  );
});

test('schema result never returns mapped field names', () => {
  const result = normalizeFeishuSchemaResult({
    valid: true,
    field_count: 12,
    mapped_field_count: 4,
    mapped_fields: ['Email', 'Secret notes'],
    provider_response: { code: 0 }
  });
  assert.deepEqual(result, { valid: true, fieldCount: 12, mappedFieldCount: 4, validatedAt: '' });
  assert.equal(JSON.stringify(result).includes('Secret notes'), false);
});

test('sync list keeps only safe ledger fields and never exposes remote record identity', () => {
  const result = normalizeFeishuBaseSyncs({
    results: [
      {
        id: SYNC_ID,
        intake_id: INTAKE_ID,
        safe_label: 'Research sync dddddddd',
        status: 'succeeded',
        external_erasure_status: 'available',
        can_delete: true,
        record_id: 'rec_private',
        app_token: 'bascn_private',
        provider_error: 'private provider body'
      },
      { id: 'bad', record_id: 'also-private' }
    ]
  });
  assert.deepEqual(result, {
    count: 1,
    results: [
      {
        id: SYNC_ID,
        intakeId: INTAKE_ID,
        safeLabel: 'Research sync dddddddd',
        status: 'succeeded',
        erasureStatus: 'available',
        canDelete: true,
        updatedAt: ''
      }
    ]
  });
  const serialized = JSON.stringify(result);
  assert.equal(serialized.includes('rec_private'), false);
  assert.equal(serialized.includes('bascn_private'), false);
  assert.equal(serialized.includes('provider body'), false);
});

test('sync projection preserves UNKNOWN and remote-erasure states', () => {
  const unknownId = 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee';
  const pendingId = 'ffffffff-ffff-4fff-8fff-ffffffffffff';
  const result = normalizeFeishuBaseSyncs({
    results: [
      {
        id: unknownId,
        status: 'unknown',
        external_erasure_status: 'unknown',
        can_delete: false
      },
      {
        id: pendingId,
        status: 'external_erasure_pending',
        external_erasure_status: 'pending',
        can_delete: true
      }
    ]
  });
  assert.deepEqual(
    result.results.map(({ status, erasureStatus, canDelete }) => ({
      status,
      erasureStatus,
      canDelete
    })),
    [
      { status: 'unknown', erasureStatus: 'unknown', canDelete: false },
      {
        status: 'external_erasure_pending',
        erasureStatus: 'pending',
        canDelete: true
      }
    ]
  );
});
