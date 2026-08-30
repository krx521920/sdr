import test from 'node:test';
import assert from 'node:assert/strict';
import { normalizeChannelSafety, safeChannel } from '../src/lib/channel-safety.js';

test('normalizer projects only safe fields and forces unsupported channels off', () => {
  const result = normalizeChannelSafety({
    environment_enabled: true,
    organization: { enabled: true, daily_limit: 10, revision: 2, secret: 'no' },
    channels: [{ channel: 'wechat', implemented: true, enabled: true, test_mode: false }],
    test_targets: [
      {
        id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
        channel: 'email',
        safe_label: 'QA ••••42',
        identifier: 'raw@example.com',
        identifier_hash: 'hash'
      }
    ],
    unknown_requests: [
      {
        id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
        channel: 'email',
        status: 'unknown',
        action: 'send',
        target_hash: 'secret'
      }
    ]
  });
  assert.equal(result.channels[0].implemented, false);
  assert.equal(result.channels[0].enabled, false);
  assert.deepEqual(result.testTargets[0], {
    id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
    channel: 'email',
    safeLabel: 'QA ••••42',
    active: false
  });
  assert.equal(JSON.stringify(result).includes('raw@example.com'), false);
  assert.equal(JSON.stringify(result).includes('hash'), false);
});

test('channel writes reject WeChat, WeCom, and unknown values', () => {
  assert.equal(safeChannel('email'), 'email');
  assert.equal(safeChannel('wechat'), '');
  assert.equal(safeChannel('wecom'), '');
  assert.equal(safeChannel('other'), '');
});
