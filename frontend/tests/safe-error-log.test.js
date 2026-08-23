import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import { getSafeErrorMetadata, logSafeServerError } from '../src/lib/server/safe-error-log.js';

const TEST_DIR = dirname(fileURLToPath(import.meta.url));

test('logs only allow-listed transport metadata from an Axios-like error', () => {
  const traceId = '0123456789abcdef0123456789abcdef';
  const error = {
    code: 'ERR_BAD_REQUEST',
    config: {
      headers: { Authorization: 'Bearer ACCESS_SECRET' },
      data: {
        refresh: 'REFRESH_SECRET',
        code: 'OAUTH_CODE_SECRET',
        code_verifier: 'PKCE_SECRET'
      }
    },
    request: {
      headers: { cookie: 'jwt_refresh=COOKIE_SECRET' },
      body: 'REQUEST_BODY_SECRET'
    },
    response: {
      status: 401,
      headers: {
        'set-cookie': 'jwt_access=RESPONSE_COOKIE_SECRET',
        'x-trace-id': traceId
      },
      data: { access_token: 'RESPONSE_TOKEN_SECRET' }
    }
  };
  error.request.circular = error;

  let logged;
  logSafeServerError('OAuth token exchange failed', error, {
    error(...args) {
      logged = args;
    }
  });

  assert.ok(logged);
  assert.deepEqual(logged, [
    'OAuth token exchange failed',
    { status: 401, code: 'ERR_BAD_REQUEST', traceId }
  ]);
  assert.deepEqual(Object.keys(logged[1]).sort(), ['code', 'status', 'traceId']);

  const serialized = JSON.stringify(logged);
  for (const secret of [
    'ACCESS_SECRET',
    'REFRESH_SECRET',
    'OAUTH_CODE_SECRET',
    'PKCE_SECRET',
    'COOKIE_SECRET',
    'REQUEST_BODY_SECRET',
    'RESPONSE_COOKIE_SECRET',
    'RESPONSE_TOKEN_SECRET'
  ]) {
    assert.equal(serialized.includes(secret), false, `${secret} leaked into the log`);
  }
});

test('rejects arbitrary and incorrectly cased error codes', () => {
  assert.deepEqual(getSafeErrorMetadata({ code: 'REFRESH_SECRET' }), {
    code: 'unknown_error'
  });
  assert.deepEqual(getSafeErrorMetadata({ code: 'err_bad_request' }), {
    code: 'unknown_error'
  });
});

test('ignores trace IDs outside trusted response headers', () => {
  const untrustedTraceId = 'abcdef0123456789abcdef0123456789';
  assert.deepEqual(
    getSafeErrorMetadata({
      code: 'ERR_BAD_REQUEST',
      traceId: untrustedTraceId,
      config: { headers: { 'x-trace-id': untrustedTraceId } }
    }),
    { code: 'ERR_BAD_REQUEST' }
  );
});
test('handles primitive values and hostile getters without throwing', () => {
  assert.deepEqual(getSafeErrorMetadata(null), { code: 'unknown_error' });
  assert.deepEqual(getSafeErrorMetadata('ACCESS_SECRET'), { code: 'unknown_error' });

  const hostile = {};
  Object.defineProperty(hostile, 'response', {
    get() {
      throw new Error('ACCESS_SECRET');
    }
  });
  assert.doesNotThrow(() => getSafeErrorMetadata(hostile));
  assert.deepEqual(getSafeErrorMetadata(hostile), { code: 'unknown_error' });
});

test('does not let a logging transport failure break authentication', () => {
  assert.doesNotThrow(() =>
    logSafeServerError('Token refresh failed', new Error('ACCESS_SECRET'), {
      error() {
        throw new Error('logger unavailable');
      }
    })
  );
});

test('authentication boundaries use only their approved static safe-log events', () => {
  const boundaries = new Map([
    ['../src/hooks.server.js', ['Token refresh failed', 'Org switch failed']],
    [
      '../src/routes/(no-layout)/login/+page.server.js',
      [
        'Google OAuth callback returned an error',
        'OAuth state validation failed',
        'OAuth PKCE verifier was unavailable',
        'OAuth token exchange failed'
      ]
    ],
    [
      '../src/routes/(no-layout)/org/+page.server.js',
      ['Organization list request failed', 'Organization switch request failed']
    ],
    ['../src/routes/(no-layout)/org/new/+page.server.js', ['Organization creation request failed']]
  ]);

  for (const [relativePath, approvedEvents] of boundaries) {
    const source = readFileSync(resolve(TEST_DIR, relativePath), 'utf8');
    assert.doesNotMatch(
      source,
      /console\.(?:error|log|warn|debug)\s*\(/,
      `${relativePath} must not bypass safe logging`
    );

    const callCount = source.match(/\blogSafeServerError\s*\(/g)?.length ?? 0;
    assert.equal(
      callCount,
      approvedEvents.length,
      `${relativePath} has an unapproved or missing safe-log event`
    );
    for (const eventName of approvedEvents) {
      assert.equal(
        source.includes(`logSafeServerError('${eventName}',`),
        true,
        `${relativePath} must use the approved static event: ${eventName}`
      );
    }
  }
});
