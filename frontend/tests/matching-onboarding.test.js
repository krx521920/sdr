import test from 'node:test';
import assert from 'node:assert/strict';

import {
  buildOnboardingEvidence,
  buildOnboardingIdentity,
  buildOnboardingPerson,
  buildPersonOnboardingPayload,
  maskIdentityValue
} from '../src/lib/matching/onboarding.js';

const personInput = {
  displayName: '  Alice Chen  ',
  currentTitle: 'Growth Engineer',
  currentCompany: 'Acme',
  location: 'Shanghai',
  availability: 'open_to_offers',
  skills: 'Python, Django\npython',
  roles: 'Growth engineer; SDR automation specialist'
};

const identityInput = {
  kind: 'email',
  value: ' Alice@Example.com ',
  source: 'linkedin',
  isPrimary: false
};

const evidenceInput = {
  kind: 'experience',
  summary: 'Built evidence-backed outbound systems.',
  source: 'ai',
  sourceUri: 'https://example.com/profile/alice',
  observedAt: '2026-08-26T08:00:00.000Z',
  validUntil: '2027-08-26T08:00:00.000Z',
  confidence: '0.8',
  skills: 'Python, Django',
  titles: 'Growth engineer',
  locations: 'Shanghai, Remote',
  availability: 'open_to_offers'
};

test('builds a bounded canonical person without arbitrary attributes', () => {
  assert.deepEqual(buildOnboardingPerson({ ...personInput, attributes: { admin: true } }), {
    payload: {
      display_name: 'Alice Chen',
      current_title: 'Growth Engineer',
      current_company: 'Acme',
      location: 'Shanghai',
      availability: 'open_to_offers',
      skills: ['Python', 'Django'],
      roles: ['Growth engineer', 'SDR automation specialist']
    }
  });
});

test('forces manual provenance and normalizes the primary identity', () => {
  assert.deepEqual(buildOnboardingIdentity(identityInput), {
    payload: {
      kind: 'email',
      normalized_value: 'alice@example.com',
      source: 'manual',
      is_primary: true
    }
  });

  assert.deepEqual(buildOnboardingIdentity({ kind: 'whatsapp', value: '+86 (138) 0013-8000' }), {
    payload: {
      kind: 'whatsapp',
      normalized_value: '+8613800138000',
      source: 'manual',
      is_primary: true
    }
  });
});

test('builds append-only evidence from the supported deterministic dimensions', () => {
  assert.deepEqual(buildOnboardingEvidence(evidenceInput), {
    payload: {
      kind: 'experience',
      source: 'manual',
      summary: 'Built evidence-backed outbound systems.',
      facts: {
        skills: ['Python', 'Django'],
        titles: ['Growth engineer'],
        locations: ['Shanghai', 'Remote'],
        availability: ['open_to_offers']
      },
      source_uri: 'https://example.com/profile/alice',
      observed_at: '2026-08-26T08:00:00.000Z',
      valid_until: '2027-08-26T08:00:00.000Z',
      confidence: 0.8
    }
  });
});

test('builds the nested backend onboarding contract', () => {
  const result = buildPersonOnboardingPayload({
    person: personInput,
    identity: identityInput,
    evidence: evidenceInput
  });

  assert.ok(result.payload);
  assert.equal(result.payload.person.display_name, 'Alice Chen');
  assert.equal(result.payload.identities.length, 1);
  assert.equal(result.payload.identities[0].normalized_value, 'alice@example.com');
  assert.equal(result.payload.identities[0].source, 'manual');
  assert.equal(result.payload.evidence.length, 1);
  assert.equal(result.payload.evidence[0].source, 'manual');
  assert.deepEqual(Object.keys(result.payload), ['person', 'identities', 'evidence']);
});

test('fails closed on unknown enums, unsafe references, dates and oversized lists', () => {
  assert.deepEqual(buildOnboardingPerson({ ...personInput, availability: 'secret' }), {
    error: 'Select a valid availability.',
    field: 'availability'
  });
  assert.deepEqual(buildOnboardingIdentity({ kind: 'cookie', value: 'abc' }), {
    error: 'Select a valid identity type.',
    field: 'identityKind'
  });
  assert.deepEqual(
    buildOnboardingEvidence({ ...evidenceInput, sourceUri: 'javascript:alert(1)' }),
    {
      error: 'Enter a safe HTTP or HTTPS reference link without credentials or tokens.',
      field: 'sourceUri'
    }
  );
  assert.deepEqual(
    buildOnboardingEvidence({
      ...evidenceInput,
      sourceUri: 'https://example.com/profile?access_token=do-not-store'
    }),
    {
      error: 'Enter a safe HTTP or HTTPS reference link without credentials or tokens.',
      field: 'sourceUri'
    }
  );
  assert.deepEqual(
    buildOnboardingEvidence({
      ...evidenceInput,
      sourceUri: 'https://example.com/callback#access_token=do-not-store'
    }),
    {
      error: 'Enter a safe HTTP or HTTPS reference link without credentials or tokens.',
      field: 'sourceUri'
    }
  );
  assert.deepEqual(
    buildOnboardingEvidence({
      ...evidenceInput,
      validUntil: '2025-08-26T08:00:00.000Z'
    }),
    { error: 'Valid until cannot precede observed at.', field: 'validUntil' }
  );
  assert.deepEqual(
    buildOnboardingPerson({
      ...personInput,
      skills: Array.from({ length: 51 }, (_, index) => `skill-${index}`).join(',')
    }),
    { error: 'Use at most 50 values in each list.', field: 'skills' }
  );
});

test('masks identity values for review without returning the original', () => {
  const emailMask = maskIdentityValue('email', 'alice@example.com');
  const phoneMask = maskIdentityValue('phone', '+8613800138000');

  assert.equal(emailMask, 'a***@example.com');
  assert.equal(phoneMask, '••••8000');
  assert.equal(emailMask.includes('alice'), false);
  assert.equal(phoneMask.includes('1380013'), false);
});
