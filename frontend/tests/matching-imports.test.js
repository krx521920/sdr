import test from 'node:test';
import assert from 'node:assert/strict';

import {
  autoMapImportHeaders,
  csvSafeCell,
  isImportBatchActive,
  isImportBatchTerminal,
  normalizeCrmImportCandidate,
  normalizeCrmImportCandidateList,
  normalizeImportBatch,
  normalizeImportRecord,
  parseCsvHeaders,
  validateCrmImportSelection,
  validateImportMapping
} from '../src/lib/matching/imports.js';

const BATCH_ID = '10000000-0000-4000-8000-000000000001';
const RECORD_ID = '10000000-0000-4000-8000-000000000002';
const PERSON_ID = '10000000-0000-4000-8000-000000000003';
const RUN_ID = '10000000-0000-4000-8000-000000000004';
const CRM_ID = '10000000-0000-4000-8000-000000000005';

test('parses quoted UTF-8 CSV headers and rejects duplicates', () => {
  assert.deepEqual(
    parseCsvHeaders('\uFEFF"Full name",Email,"Evidence, summary"\r\nAlice,a@test,x'),
    {
      headers: ['Full name', 'Email', 'Evidence, summary']
    }
  );
  assert.match(parseCsvHeaders('Email,email\n')?.error || '', /unique/i);
  assert.match(parseCsvHeaders('"broken\n')?.error || '', /quote/i);
});

test('auto maps common aliases and requires a person identity', () => {
  const headers = ['Name', 'Email', 'Company', 'Notes'];
  assert.deepEqual(autoMapImportHeaders(headers), {
    display_name: 'Name',
    current_company: 'Company',
    email: 'Email',
    evidence_summary: 'Notes'
  });
  assert.deepEqual(validateImportMapping({ display_name: 'Name' }, headers), {
    error: 'Map at least one identity column.',
    field: 'identity'
  });
  assert.deepEqual(validateImportMapping({ display_name: 'Name', email: 'Email' }, headers), {
    mapping: { display_name: 'Name', email: 'Email' }
  });
});

test('mapping ignores unknown targets and rejects reused or stale headers', () => {
  const headers = ['Name', 'Email'];
  assert.deepEqual(
    validateImportMapping(
      { display_name: 'Name', email: 'Email', source: 'Email', payload: 'Name' },
      headers
    ),
    { mapping: { display_name: 'Name', email: 'Email' } }
  );
  assert.match(
    validateImportMapping({ display_name: 'Name', email: 'Name' }, headers)?.error || '',
    /only be mapped once/i
  );
  assert.match(
    validateImportMapping({ display_name: 'Name', email: 'Missing' }, headers)?.error || '',
    /no longer available/i
  );
});

test('normalizes CRM candidates without copying raw identities or provider payloads', () => {
  const candidate = normalizeCrmImportCandidate({
    id: CRM_ID,
    entity_type: 'lead',
    display_name: 'Alice',
    current_title: 'CTO',
    company_name: 'Acme',
    masked_identities: [
      { kind: 'email', masked_value: 'a***@example.com', value: 'alice@example.com' },
      { kind: 'phone', display_value: '+15551234567' },
      { kind: 'email', value: 'second@example.com' }
    ],
    email: 'alice@example.com',
    phone: '+15551234567',
    raw_payload: { secret: true }
  });
  assert.equal(candidate.id, CRM_ID);
  assert.equal(candidate.currentCompany, 'Acme');
  assert.deepEqual(candidate.maskedIdentities, [
    { kind: 'email', maskedValue: 'a***@example.com' }
  ]);
  const serialized = JSON.stringify(candidate);
  assert.equal(serialized.includes('alice@example.com'), false);
  assert.equal(serialized.includes('+15551234567'), false);
  assert.equal(serialized.includes('second@example.com'), false);
  assert.equal(serialized.includes('secret'), false);
});

test('bounds CRM candidate lists and validates deduplicated preview selections', () => {
  const list = normalizeCrmImportCandidateList({
    count: 200,
    results: [
      { id: CRM_ID, entity_type: 'contact', display_name: 'Alice' },
      { id: 'invalid', entity_type: 'contact', display_name: 'Invalid' }
    ],
    identities: ['private@example.com']
  });
  assert.equal(list.count, 200);
  assert.deepEqual(
    list.results.map((candidate) => candidate.id),
    [CRM_ID]
  );
  assert.deepEqual(validateCrmImportSelection('contact', [CRM_ID, CRM_ID]), {
    payload: { entity_type: 'contact', record_ids: [CRM_ID] }
  });
  assert.match(validateCrmImportSelection('account', [CRM_ID]).error || '', /Leads or Contacts/i);
  assert.match(validateCrmImportSelection('lead', []).error || '', /at least one/i);
  assert.match(validateCrmImportSelection('lead', ['invalid']).error || '', /invalid/i);
});

test('normalizes import batches through a strict safe projection', () => {
  const normalized = normalizeImportBatch({
    id: BATCH_ID,
    status: 'running',
    revision: 4,
    file_name: 'people.csv',
    headers: ['Name', 'Email'],
    mapping: { display_name: 'Name', email: 'Email', source: 'Email' },
    source: 'crm',
    source_namespace: 'crm:lead',
    counts: { total: 2, valid: 1, conflicts: 1 },
    job_status: 'running',
    match_run_ids: [RUN_ID, RUN_ID, 'not-a-uuid'],
    replayed: true,
    payload: { identities: ['alice@example.com'] },
    result: { facts: { skills: ['private'] } },
    last_error_message: 'private provider response',
    records: []
  });

  assert.equal(normalized.id, BATCH_ID);
  assert.equal(normalized.counts.ready, 1);
  assert.equal(normalized.counts.conflict, 1);
  assert.equal(normalized.jobStatus, 'running');
  assert.deepEqual(normalized.matchRunIds, [RUN_ID]);
  assert.deepEqual(normalized.mapping, { display_name: 'Name', email: 'Email' });
  assert.equal(normalized.source, 'crm');
  assert.equal(normalized.sourceNamespace, 'crm:lead');
  assert.equal(normalized.sourceLabel, 'Internal CRM · Leads');
  const serialized = JSON.stringify(normalized);
  assert.equal(serialized.includes('alice@example.com'), false);
  assert.equal(serialized.includes('facts'), false);
  assert.equal(serialized.includes('provider response'), false);
  assert.equal(isImportBatchActive(normalized), true);
  assert.equal(isImportBatchActive({ status: 'running', jobStatus: 'dead_letter' }), false);
  assert.equal(isImportBatchActive({ status: 'queued', jobStatus: 'cancelled' }), false);
  assert.equal(isImportBatchActive({ status: 'running', jobStatus: 'succeeded' }), false);
  assert.equal(isImportBatchTerminal({ status: 'completed' }), true);
});

test('sums created and merged records for backend batch counts', () => {
  const normalized = normalizeImportBatch({
    id: BATCH_ID,
    status: 'partial',
    counts: { total: 4, processed: 4, created: 2, merged: 1, failed: 1 }
  });
  assert.equal(normalized.counts.imported, 3);
  assert.equal(isImportBatchTerminal(normalized), true);
});

test('projects destination-scoped Feishu namespaces without exposing the scope digest', () => {
  const normalized = normalizeImportBatch({
    id: BATCH_ID,
    status: 'previewed',
    source: 'feishu',
    source_namespace: `feishu:base:${'a'.repeat(64)}`
  });
  assert.equal(normalized.source, 'feishu');
  assert.equal(normalized.sourceNamespace, 'feishu:base');
  assert.equal(normalized.sourceLabel, 'Feishu Base');
  assert.equal(JSON.stringify(normalized).includes('a'.repeat(64)), false);

  const shortened = normalizeImportBatch({
    id: BATCH_ID,
    source: 'feishu',
    source_namespace: `feishu:base:${'b'.repeat(32)}`
  });
  assert.equal(shortened.sourceNamespace, 'feishu:base');
  assert.equal(shortened.sourceLabel, 'Feishu Base');
  assert.equal(JSON.stringify(shortened).includes('b'.repeat(32)), false);
});

test('projects inbound Email previews without address or mailbox scope in browser state', () => {
  const rawAddress = 'private-correspondent@example.com';
  const scopeDigest = 'c'.repeat(64);
  const normalized = normalizeImportBatch({
    id: BATCH_ID,
    status: 'previewed',
    source: 'email',
    source_namespace: `email:inbound:${scopeDigest}`,
    original_filename: `email:inbound:${scopeDigest}.json`,
    records: [
      {
        id: RECORD_ID,
        status: 'ready',
        display_name: 'Email correspondent',
        masked_identities: [
          {
            kind: 'email',
            present: true,
            masked_value: '',
            display_value: rawAddress,
            value: rawAddress
          }
        ]
      }
    ]
  });

  assert.equal(normalized.sourceNamespace, 'email:inbound');
  assert.equal(normalized.sourceLabel, 'Inbound Email');
  assert.equal(normalized.fileName, 'Inbound email preview');
  assert.deepEqual(normalized.records[0].identities, [
    { kind: 'email', maskedValue: '', present: true }
  ]);
  const serialized = JSON.stringify(normalized);
  assert.equal(serialized.includes(rawAddress), false);
  assert.equal(serialized.includes(scopeDigest), false);
});

test('projects exhausted or cancelled jobs as failed without guessing succeeded batches', () => {
  assert.equal(
    normalizeImportBatch({ id: BATCH_ID, status: 'running', job_status: 'dead_letter' }).status,
    'failed'
  );
  assert.equal(
    normalizeImportBatch({ id: BATCH_ID, status: 'queued', job_status: 'cancelled' }).status,
    'failed'
  );
  assert.equal(
    normalizeImportBatch({ id: BATCH_ID, status: 'running', job_status: 'succeeded' }).status,
    'running'
  );
});

test('normalizes records without raw identity, facts or source references', () => {
  const normalized = normalizeImportRecord({
    id: RECORD_ID,
    row_number: 7,
    status: 'conflict',
    revision: 3,
    person_summary: {
      id: PERSON_ID,
      display_name: 'Alice',
      current_title: 'Engineer',
      raw_identity: 'private@example.com'
    },
    identity_summaries: [{ kind: 'email', masked_value: 'a***@example.com', value: 'private' }],
    evidence_summary: {
      kind: 'profile',
      summary: 'Public profile summary',
      facts: { skills: ['private'] },
      source_uri: 'https://private.example'
    },
    conflict: {
      code: 'identity_conflict',
      existing_person: { id: PERSON_ID, display_name: 'Existing Alice' }
    },
    payload: { secret: true }
  });

  assert.equal(normalized.identities[0].maskedValue, 'a***@example.com');
  assert.equal(normalized.conflict.existingPerson.displayName, 'Existing Alice');
  const serialized = JSON.stringify(normalized);
  assert.equal(serialized.includes('private@example.com'), false);
  assert.equal(serialized.includes('private.example'), false);
  assert.equal(serialized.includes('facts'), false);
  assert.equal(serialized.includes('secret'), false);
});

test('does not reuse an import record id as a missing person id', () => {
  const normalized = normalizeImportRecord({
    id: RECORD_ID,
    status: 'ready',
    display_name: 'Pending Alice',
    person_summary: null
  });
  assert.equal(normalized.person.id, '');
  assert.equal(normalized.person.displayName, 'Pending Alice');
});

test('creates RFC4180 cells and neutralizes spreadsheet formulas', () => {
  assert.equal(csvSafeCell('plain'), 'plain');
  assert.equal(csvSafeCell('a,"b"'), '"a,""b"""');
  assert.equal(csvSafeCell('=HYPERLINK("bad")'), '"\'=HYPERLINK(""bad"")"');
  assert.equal(csvSafeCell('@SUM(A1)'), "'@SUM(A1)");
});
