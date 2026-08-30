import assert from 'node:assert/strict';
import test from 'node:test';

import {
  buildWhatsAppApprovedExecution,
  normalizeWhatsAppApprovalRequired,
  normalizeWhatsAppConnection,
  normalizeWhatsAppExecutionResult,
  normalizeWhatsAppMessageResponse
} from '../src/lib/whatsapp-execution.js';

const CAMPAIGN_ID = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const PROSPECT_ID = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
const MESSAGE_ID = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc';
const APPROVAL_ID = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd';
const REQUEST_ID = 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee';
const JOB_ID = 'ffffffff-ffff-4fff-8fff-ffffffffffff';
const TARGET_HASH = 'a'.repeat(64);
const PAYLOAD_HASH = 'b'.repeat(64);

test('message list is campaign-bound and strips every provider and PII field', () => {
  const result = normalizeWhatsAppMessageResponse(
    {
      count: 999,
      results: [
        {
          id: MESSAGE_ID,
          campaign_id: CAMPAIGN_ID,
          prospect_id: PROSPECT_ID,
          status: 'pending',
          execution_request_id: null,
          execution_status: null,
          created_at: '2026-08-30T01:02:03Z',
          recipient: '+15551234567',
          template_name: 'private-template',
          provider_message_id: 'wamid.private',
          error_message: 'provider body',
          payload_snapshot: { private: true }
        },
        {
          id: '11111111-1111-4111-8111-111111111111',
          campaign_id: '22222222-2222-4222-8222-222222222222',
          prospect_id: PROSPECT_ID,
          status: 'pending'
        }
      ]
    },
    CAMPAIGN_ID
  );

  assert.deepEqual(result, {
    count: 1,
    results: [
      {
        id: MESSAGE_ID,
        campaignId: CAMPAIGN_ID,
        prospectId: PROSPECT_ID,
        status: 'pending',
        executionRequestId: null,
        executionStatus: null,
        createdAt: '2026-08-30T01:02:03Z'
      }
    ]
  });
  const serialized = JSON.stringify(result);
  for (const secret of ['+15551234567', 'private-template', 'wamid.private', 'provider body']) {
    assert.equal(serialized.includes(secret), false);
  }
});

test('message projection preserves bound UNKNOWN without exposing an error', () => {
  const result = normalizeWhatsAppMessageResponse(
    {
      results: [
        {
          id: MESSAGE_ID,
          campaign_id: CAMPAIGN_ID,
          prospect_id: PROSPECT_ID,
          status: 'unknown',
          execution_request_id: REQUEST_ID,
          execution_status: 'unknown',
          created_at: '',
          error_code: 'must-not-leak'
        }
      ]
    },
    CAMPAIGN_ID
  );
  assert.equal(result.results[0].executionStatus, 'unknown');
  assert.equal(result.results[0].createdAt, '');
  assert.equal(JSON.stringify(result).includes('must-not-leak'), false);
});

test('approval intent is exact, message-bound, and non-PII', () => {
  const intent = normalizeWhatsAppApprovalRequired(
    {
      approval_required: true,
      intent: {
        channel: 'whatsapp',
        action: 'send_message',
        message_id: MESSAGE_ID,
        target_sha256: TARGET_HASH,
        payload_sha256: PAYLOAD_HASH,
        units: 1,
        recipient: '+15551234567',
        template_name: 'private-template'
      }
    },
    MESSAGE_ID
  );
  assert.deepEqual(intent, {
    action: 'send_message',
    messageId: MESSAGE_ID,
    targetHash: TARGET_HASH,
    payloadHash: PAYLOAD_HASH,
    units: 1
  });
  assert.equal(JSON.stringify(intent).includes('+15551234567'), false);
  assert.equal(JSON.stringify(intent).includes('private-template'), false);
});

test('approval intent rejects message, hash, action, channel, and unit mismatches', () => {
  const response = {
    approval_required: true,
    intent: {
      channel: 'whatsapp',
      action: 'send_message',
      message_id: MESSAGE_ID,
      target_sha256: TARGET_HASH,
      payload_sha256: PAYLOAD_HASH,
      units: 1
    }
  };
  assert.ok(normalizeWhatsAppApprovalRequired(response, MESSAGE_ID));
  assert.equal(
    normalizeWhatsAppApprovalRequired(
      { ...response, intent: { ...response.intent, message_id: PROSPECT_ID } },
      MESSAGE_ID
    ),
    null
  );
  assert.equal(
    normalizeWhatsAppApprovalRequired(
      { ...response, intent: { ...response.intent, payload_sha256: 'bad' } },
      MESSAGE_ID
    ),
    null
  );
  assert.equal(
    normalizeWhatsAppApprovalRequired(
      { ...response, intent: { ...response.intent, action: 'send_messages' } },
      MESSAGE_ID
    ),
    null
  );
  assert.equal(
    normalizeWhatsAppApprovalRequired(
      { ...response, intent: { ...response.intent, channel: 'email' } },
      MESSAGE_ID
    ),
    null
  );
  assert.equal(
    normalizeWhatsAppApprovalRequired(
      { ...response, intent: { ...response.intent, units: 2 } },
      MESSAGE_ID
    ),
    null
  );
});

test('approved body contains only the one-time approval UUID', () => {
  assert.deepEqual(buildWhatsAppApprovedExecution(APPROVAL_ID), {
    approval_id: APPROVAL_ID
  });
  assert.equal(buildWhatsAppApprovedExecution('not-a-uuid'), null);
});

test('connection projection keeps only configured state and required non-secret UI fields', () => {
  const result = normalizeWhatsAppConnection({
    phone_number_id: '123456789',
    business_account_id: '987654321',
    display_phone_number: '+1 555 123 4567',
    is_active: true,
    access_token_configured: true,
    access_token_hint: 'SECRET42',
    access_token: 'raw-secret',
    encrypted_access_token: 'ciphertext',
    message_summary: { sent: 4, delivered: 3, failed: 1, provider_detail: 'private' }
  });

  assert.deepEqual(result, {
    phone_number_id: '123456789',
    business_account_id: '987654321',
    display_phone_number: '+1 555 123 4567',
    is_active: true,
    access_token_configured: true,
    message_summary: { sent: 4, delivered: 3, failed: 1 }
  });
  const serialized = JSON.stringify(result);
  for (const secret of ['SECRET42', 'raw-secret', 'ciphertext', 'private']) {
    assert.equal(serialized.includes(secret), false);
  }
});

test('execution acknowledgement keeps only durable state identifiers', () => {
  const result = normalizeWhatsAppExecutionResult({
    job_id: JOB_ID,
    status: 'queued',
    execution_request_id: REQUEST_ID,
    execution_status: 'reserved',
    replayed: false,
    recipient: '+15551234567',
    provider_payload: { private: true }
  });
  assert.deepEqual(result, {
    executionRequestId: REQUEST_ID,
    executionStatus: 'reserved',
    jobStatus: 'queued',
    replayed: false,
    jobId: JOB_ID
  });
  assert.equal(JSON.stringify(result).includes('+15551234567'), false);
});

test('fresh reservation accepts only active idempotent job states', () => {
  for (const status of ['pending', 'queued', 'running', 'retry_scheduled']) {
    assert.deepEqual(
      normalizeWhatsAppExecutionResult({
        job_id: JOB_ID,
        status,
        execution_request_id: REQUEST_ID,
        execution_status: 'reserved',
        replayed: status !== 'pending'
      }),
      {
        executionRequestId: REQUEST_ID,
        executionStatus: 'reserved',
        jobStatus: status,
        replayed: status !== 'pending',
        jobId: JOB_ID
      }
    );
  }

  for (const status of ['succeeded', 'failed', 'unknown', 'cancelled', 'retrying', '']) {
    assert.equal(
      normalizeWhatsAppExecutionResult({
        job_id: JOB_ID,
        status,
        execution_request_id: REQUEST_ID,
        execution_status: 'reserved',
        replayed: true
      }),
      null
    );
  }
});

test('accepted idempotent replay converges without claiming a new job', () => {
  assert.deepEqual(
    normalizeWhatsAppExecutionResult({
      code: 'whatsapp_execution_not_replayable',
      execution_request_id: REQUEST_ID,
      execution_status: 'accepted',
      replayed: true,
      provider_message_id: 'must-not-leak'
    }),
    {
      executionRequestId: REQUEST_ID,
      executionStatus: 'accepted',
      jobStatus: null,
      replayed: true,
      jobId: null
    }
  );
});
