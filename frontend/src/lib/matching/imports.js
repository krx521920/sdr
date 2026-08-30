const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export const MATCHING_IMPORT_MAX_BYTES = 5 * 1024 * 1024;
export const MATCHING_IMPORT_MAX_ROWS = 500;
export const MATCHING_CRM_PAGE_SIZE = 100;
export const MATCHING_CRM_ENTITY_TYPES = ['lead', 'contact'];

export const MATCHING_IMPORT_FIELDS = [
  { target: 'display_name', label: 'Display name', group: 'Person', required: true },
  { target: 'first_name', label: 'First name', group: 'Person' },
  { target: 'last_name', label: 'Last name', group: 'Person' },
  { target: 'headline', label: 'Headline', group: 'Person' },
  { target: 'summary', label: 'Profile summary', group: 'Person' },
  { target: 'current_title', label: 'Current title', group: 'Person' },
  { target: 'current_company', label: 'Current company', group: 'Person' },
  { target: 'location', label: 'Location', group: 'Person' },
  { target: 'timezone', label: 'Timezone', group: 'Person' },
  { target: 'availability', label: 'Availability', group: 'Person' },
  { target: 'skills', label: 'Skills', group: 'Person' },
  { target: 'roles', label: 'Roles', group: 'Person' },
  { target: 'email', label: 'Email', group: 'Identity', identity: true },
  { target: 'phone', label: 'Phone', group: 'Identity', identity: true },
  { target: 'linkedin', label: 'LinkedIn', group: 'Identity', identity: true },
  { target: 'whatsapp', label: 'WhatsApp', group: 'Identity', identity: true },
  { target: 'wechat', label: 'WeChat', group: 'Identity', identity: true },
  { target: 'external_id', label: 'External ID', group: 'Identity', identity: true },
  { target: 'source_record_id', label: 'Source record ID', group: 'Evidence' },
  { target: 'evidence_summary', label: 'Evidence summary', group: 'Evidence' },
  { target: 'evidence_kind', label: 'Evidence kind', group: 'Evidence' },
  { target: 'confidence', label: 'Confidence', group: 'Evidence' },
  { target: 'observed_at', label: 'Observed at', group: 'Evidence' },
  { target: 'source_uri', label: 'Reference URL', group: 'Evidence' }
];

const TARGETS = new Set(MATCHING_IMPORT_FIELDS.map((field) => field.target));
const IDENTITY_TARGETS = new Set(
  MATCHING_IMPORT_FIELDS.filter((field) => field.identity).map((field) => field.target)
);

const ACTIVE_STATUSES = new Set(['queued', 'running']);
const TERMINAL_STATUSES = new Set(['completed', 'partial', 'failed']);
const KNOWN_STATUSES = new Set([...ACTIVE_STATUSES, ...TERMINAL_STATUSES, 'previewed']);
const KNOWN_JOB_STATUSES = new Set([
  'pending',
  'queued',
  'running',
  'retry_scheduled',
  'succeeded',
  'dead_letter',
  'cancelled'
]);
const TERMINAL_JOB_STATUSES = new Set(['succeeded', 'dead_letter', 'cancelled']);
const RECORD_STATUSES = new Set([
  'ready',
  'conflict',
  'invalid',
  'skipped',
  'created',
  'merged',
  'replayed',
  'failed'
]);
const IMPORT_SOURCES = new Set([
  'crm',
  'apollo',
  'linkedin',
  'whatsapp',
  'wechat',
  'feishu',
  'email',
  'manual',
  'ai',
  'other'
]);
const SAFE_SOURCE_NAMESPACES = new Map([
  ['manual:csv', 'CSV file'],
  ['crm:lead', 'Internal CRM · Leads'],
  ['crm:contact', 'Internal CRM · Contacts'],
  ['feishu:base', 'Feishu Base'],
  ['email:inbound', 'Inbound Email']
]);
const FEISHU_SCOPED_NAMESPACE = /^feishu:base:[0-9a-f]{32}(?:[0-9a-f]{32})?$/;
const EMAIL_SCOPED_NAMESPACE = /^email:inbound(?::[0-9a-f]{32}(?:[0-9a-f]{32})?)?$/;

const HEADER_ALIASES = {
  display_name: ['display_name', 'display name', 'full_name', 'full name', 'name'],
  first_name: ['first_name', 'first name', 'given_name', 'given name'],
  last_name: ['last_name', 'last name', 'family_name', 'family name', 'surname'],
  headline: ['headline'],
  summary: ['summary', 'profile_summary', 'profile summary', 'bio'],
  current_title: ['current_title', 'current title', 'job_title', 'job title', 'title'],
  current_company: ['current_company', 'current company', 'company', 'organization'],
  location: ['location', 'city'],
  timezone: ['timezone', 'time_zone', 'time zone'],
  availability: ['availability', 'availability_status', 'availability status'],
  skills: ['skills', 'skill'],
  roles: ['roles', 'role'],
  email: ['email', 'email_address', 'email address'],
  phone: ['phone', 'phone_number', 'phone number', 'mobile'],
  linkedin: ['linkedin', 'linkedin_url', 'linkedin url'],
  whatsapp: ['whatsapp', 'whatsapp_number', 'whatsapp number'],
  wechat: ['wechat', 'wechat_id', 'wechat id', 'weixin'],
  external_id: ['external_id', 'external id'],
  source_record_id: ['source_record_id', 'source record id', 'record_id', 'record id'],
  evidence_summary: ['evidence_summary', 'evidence summary', 'evidence', 'notes'],
  evidence_kind: ['evidence_kind', 'evidence kind'],
  confidence: ['confidence'],
  observed_at: ['observed_at', 'observed at', 'observed_date', 'observed date'],
  source_uri: ['source_uri', 'source uri', 'source_url', 'source url', 'reference_url']
};

/** @param {unknown} value */
function plainObject(value) {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

/** @param {unknown} value */
function boundedText(value, max = 255) {
  return typeof value === 'string' ? value.trim().slice(0, max) : '';
}

/** @param {unknown} value */
function safeInteger(value) {
  const number = Number(value);
  return Number.isFinite(number) ? Math.max(0, Math.trunc(number)) : 0;
}

/** @param {unknown} value */
function safeDate(value) {
  if (typeof value !== 'string' || !value.trim()) return '';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? '' : parsed.toISOString();
}

/** @param {unknown} value */
export function isUuid(value) {
  return typeof value === 'string' && UUID_RE.test(value);
}

/**
 * Read only the first CSV record. Quotes and escaped quotes are supported so
 * browser-side mapping does not split legitimate header names.
 *
 * @param {unknown} source
 * @returns {{ headers?: string[], error?: string }}
 */
export function parseCsvHeaders(source) {
  if (typeof source !== 'string' || !source) return { error: 'The CSV file is empty.' };
  const text = source.replace(/^\uFEFF/, '');
  const headers = [];
  let value = '';
  let quoted = false;
  let index = 0;

  while (index < text.length) {
    const character = text[index];
    if (quoted) {
      if (character === '"' && text[index + 1] === '"') {
        value += '"';
        index += 2;
        continue;
      }
      if (character === '"') {
        quoted = false;
        index += 1;
        continue;
      }
      value += character;
      index += 1;
      continue;
    }
    if (character === '"' && value === '') {
      quoted = true;
      index += 1;
      continue;
    }
    if (character === ',') {
      headers.push(value.trim());
      value = '';
      index += 1;
      continue;
    }
    if (character === '\n' || character === '\r') break;
    value += character;
    index += 1;
  }
  if (quoted) return { error: 'The CSV header contains an unclosed quote.' };
  headers.push(value.trim());

  if (headers.length > 100) return { error: 'Use at most 100 CSV columns.' };
  if (headers.some((header) => !header || header.length > 128)) {
    return { error: 'Every CSV column needs a header of at most 128 characters.' };
  }
  const normalized = headers.map((header) => header.toLocaleLowerCase());
  if (new Set(normalized).size !== headers.length) {
    return { error: 'CSV column headers must be unique.' };
  }
  return { headers };
}

/** @param {string[]} headers */
export function autoMapImportHeaders(headers) {
  const lookup = new Map(headers.map((header) => [header.trim().toLocaleLowerCase(), header]));
  /** @type {Record<string, string>} */
  const mapping = {};
  const used = new Set();
  for (const field of MATCHING_IMPORT_FIELDS) {
    const candidate = (HEADER_ALIASES[field.target] || [])
      .map((alias) => lookup.get(alias))
      .find((header) => header && !used.has(header));
    if (candidate) {
      mapping[field.target] = candidate;
      used.add(candidate);
    }
  }
  return mapping;
}

/**
 * @param {unknown} value
 * @param {string[]} headers
 * @returns {{ mapping?: Record<string, string>, error?: string, field?: string }}
 */
export function validateImportMapping(value, headers) {
  if (!plainObject(value)) return { error: 'Map the CSV columns before continuing.' };
  const availableHeaders = new Set(headers);
  const usedHeaders = new Set();
  /** @type {Record<string, string>} */
  const mapping = {};

  for (const [target, rawHeader] of Object.entries(
    /** @type {Record<string, unknown>} */ (value)
  )) {
    if (!TARGETS.has(target)) continue;
    const header = boundedText(rawHeader, 128);
    if (!header) continue;
    if (!availableHeaders.has(header)) {
      return { error: `The mapped column for ${target} is no longer available.`, field: target };
    }
    if (usedHeaders.has(header)) {
      return { error: `The column “${header}” can only be mapped once.`, field: target };
    }
    usedHeaders.add(header);
    mapping[target] = header;
  }

  if (!mapping.display_name) {
    return { error: 'Map a CSV column to Display name.', field: 'display_name' };
  }
  if (![...IDENTITY_TARGETS].some((target) => mapping[target])) {
    return { error: 'Map at least one identity column.', field: 'identity' };
  }
  return { mapping };
}

/** @param {unknown} raw */
function normalizeCounts(raw) {
  const value = plainObject(raw) ? /** @type {Record<string, unknown>} */ (raw) : {};
  const imported =
    value.imported === undefined
      ? safeInteger(value.created) + safeInteger(value.merged)
      : safeInteger(value.imported);
  return {
    total: safeInteger(value.total),
    processed: safeInteger(value.processed),
    ready: safeInteger(value.ready ?? value.valid),
    conflict: safeInteger(value.conflict ?? value.conflicts),
    invalid: safeInteger(value.invalid),
    skipped: safeInteger(value.skipped),
    imported,
    failed: safeInteger(value.failed)
  };
}

/** @param {unknown} raw */
function normalizePersonSummary(raw) {
  const value = plainObject(raw) ? /** @type {Record<string, unknown>} */ (raw) : {};
  return {
    id: isUuid(value.id) ? String(value.id) : '',
    displayName: boundedText(value.display_name ?? value.displayName, 255) || 'Unnamed person',
    currentTitle: boundedText(value.current_title ?? value.currentTitle, 255),
    currentCompany: boundedText(value.current_company ?? value.currentCompany, 255),
    location: boundedText(value.location, 255)
  };
}

/** @param {unknown} raw */
function normalizeIdentitySummaries(raw) {
  if (!Array.isArray(raw)) return [];
  return raw
    .slice(0, 20)
    .map((item) => {
      const value = plainObject(item) ? /** @type {Record<string, unknown>} */ (item) : {};
      const maskedValue = boundedText(value.masked_value ?? value.maskedValue, 128);
      return {
        kind: boundedText(value.kind, 32),
        maskedValue: maskedValue.includes('***') ? maskedValue : '',
        present: value.present === true
      };
    })
    .filter((identity) => identity.kind && (identity.maskedValue || identity.present));
}

/**
 * Safe CRM candidate projection. Raw email/phone fields and CRM payloads are
 * intentionally ignored; the backend must provide already-masked identities.
 *
 * @param {unknown} raw
 */
export function normalizeCrmImportCandidate(raw) {
  const value = plainObject(raw) ? /** @type {Record<string, unknown>} */ (raw) : {};
  const entityType = boundedText(value.entity_type, 16).toLowerCase();
  const identityRows = value.identity_summaries ?? value.masked_identities ?? value.identities;
  const maskedIdentities = (Array.isArray(identityRows) ? identityRows : [])
    .slice(0, 20)
    .map((item) => {
      const identity = plainObject(item) ? /** @type {Record<string, unknown>} */ (item) : {};
      return {
        kind: boundedText(identity.kind, 32),
        maskedValue: boundedText(identity.masked_value ?? identity.maskedValue, 128)
      };
    })
    .filter((identity) => identity.maskedValue);
  return {
    id: isUuid(value.id) ? String(value.id) : '',
    entityType: MATCHING_CRM_ENTITY_TYPES.includes(entityType) ? entityType : '',
    displayName: boundedText(value.display_name, 255) || 'Unnamed CRM record',
    currentTitle: boundedText(value.current_title ?? value.title, 255),
    currentCompany: boundedText(
      value.current_company ?? value.company_name ?? value.account_name,
      255
    ),
    maskedIdentities
  };
}

/** @param {unknown} raw */
export function normalizeCrmImportCandidateList(raw) {
  const value = plainObject(raw) ? /** @type {Record<string, unknown>} */ (raw) : {};
  const items = Array.isArray(raw) ? raw : Array.isArray(value.results) ? value.results : [];
  return {
    count: safeInteger(value.count ?? items.length),
    results: items
      .map(normalizeCrmImportCandidate)
      .filter((candidate) => candidate.id)
      .slice(0, MATCHING_CRM_PAGE_SIZE)
  };
}

/**
 * @param {unknown} entityType
 * @param {unknown} recordIds
 * @returns {{ payload?: {entity_type:string,record_ids:string[]}, error?: string }}
 */
export function validateCrmImportSelection(entityType, recordIds) {
  const normalizedType = boundedText(entityType, 16).toLowerCase();
  if (!MATCHING_CRM_ENTITY_TYPES.includes(normalizedType)) {
    return { error: 'Choose CRM Leads or Contacts.' };
  }
  if (!Array.isArray(recordIds)) return { error: 'Select at least one CRM record.' };
  const uniqueIds = [...new Set(recordIds.map((id) => boundedText(id, 64)).filter(Boolean))];
  if (uniqueIds.length === 0) return { error: 'Select at least one CRM record.' };
  if (uniqueIds.length > MATCHING_IMPORT_MAX_ROWS) {
    return { error: `Select no more than ${MATCHING_IMPORT_MAX_ROWS} CRM records.` };
  }
  if (uniqueIds.some((id) => !isUuid(id))) {
    return { error: 'One or more selected CRM records are invalid.' };
  }
  return { payload: { entity_type: normalizedType, record_ids: uniqueIds } };
}

/** @param {unknown} raw */
function normalizeRecordErrors(raw) {
  if (!Array.isArray(raw)) return [];
  return raw.slice(0, 50).map((item) => {
    if (typeof item === 'string') {
      return { field: '', code: '', message: boundedText(item, 500) || 'Review this row.' };
    }
    const value = plainObject(item) ? /** @type {Record<string, unknown>} */ (item) : {};
    return {
      field: boundedText(value.field, 64),
      code: boundedText(value.code, 64),
      message: boundedText(value.message ?? value.detail, 500) || 'Review this row.'
    };
  });
}

/** @param {unknown} raw */
export function normalizeImportRecord(raw) {
  const value = plainObject(raw) ? /** @type {Record<string, unknown>} */ (raw) : {};
  const status = boundedText(value.status, 32).toLowerCase();
  const conflictValue = plainObject(value.conflict)
    ? /** @type {Record<string, unknown>} */ (value.conflict)
    : {};
  const existingRaw = value.existing_person ?? conflictValue.existing_person;
  const candidateRaw =
    conflictValue.candidates ??
    conflictValue.people ??
    value.conflict_candidates ??
    value.candidate_people;
  const candidates = (Array.isArray(candidateRaw) ? candidateRaw : [])
    .map(normalizePersonSummary)
    .filter((person) => person.id);
  const existingPerson = normalizePersonSummary(existingRaw);
  if (existingPerson.id && !candidates.some((person) => person.id === existingPerson.id)) {
    candidates.unshift(existingPerson);
  }
  const personRaw = plainObject(value.person_summary)
    ? value.person_summary
    : plainObject(value.person)
      ? value.person
      : { display_name: value.display_name };
  const evidenceRaw = plainObject(value.evidence_summary)
    ? /** @type {Record<string, unknown>} */ (value.evidence_summary)
    : {};
  return {
    id: isUuid(value.id) ? String(value.id) : '',
    rowNumber: safeInteger(value.row_number ?? value.row),
    status: RECORD_STATUSES.has(status) ? status : '',
    revision: safeInteger(value.revision),
    person: normalizePersonSummary(personRaw),
    identities: normalizeIdentitySummaries(
      value.identity_summaries ?? value.masked_identities ?? value.identities
    ),
    evidence: {
      kind: boundedText(evidenceRaw.kind ?? value.evidence_kind, 32),
      summary: boundedText(
        evidenceRaw.summary ??
          value.evidence_summary_text ??
          (typeof value.evidence_summary === 'string' ? value.evidence_summary : ''),
        1000
      ),
      confidence: Math.max(0, Math.min(1, Number(evidenceRaw.confidence) || 0)),
      observedAt: safeDate(evidenceRaw.observed_at)
    },
    errors: normalizeRecordErrors(value.errors ?? value.field_errors),
    conflict: {
      code: boundedText(conflictValue.code ?? value.conflict_code, 64),
      revision: safeInteger(conflictValue.revision ?? value.conflict_revision ?? value.revision),
      candidates,
      existingPerson
    }
  };
}

/** @param {unknown} raw */
export function normalizeImportBatch(raw) {
  const value = plainObject(raw) ? /** @type {Record<string, unknown>} */ (raw) : {};
  const status = boundedText(value.status, 32).toLowerCase();
  const headers = Array.isArray(value.headers)
    ? value.headers
        .map((header) => boundedText(header, 128))
        .filter(Boolean)
        .slice(0, 100)
    : [];
  const mappingResult = validateImportMapping(value.mapping, headers);
  const recordsRaw = Array.isArray(value.records) ? value.records : [];
  const jobStatus = boundedText(value.job_status, 32).toLowerCase();
  const normalizedJobStatus = KNOWN_JOB_STATUSES.has(jobStatus) ? jobStatus : '';
  const normalizedStatus = KNOWN_STATUSES.has(status) ? status : '';
  const projectedStatus =
    ['dead_letter', 'cancelled'].includes(normalizedJobStatus) &&
    !TERMINAL_STATUSES.has(normalizedStatus)
      ? 'failed'
      : normalizedStatus;
  const matchRunIds = Array.isArray(value.match_run_ids)
    ? [...new Set(value.match_run_ids.filter(isUuid).map(String))].slice(
        0,
        MATCHING_IMPORT_MAX_ROWS
      )
    : [];
  const requestedSource = boundedText(value.source, 24).toLowerCase();
  const source = IMPORT_SOURCES.has(requestedSource) ? requestedSource : '';
  const requestedNamespace = boundedText(value.source_namespace, 128).toLowerCase();
  const sourceNamespace = SAFE_SOURCE_NAMESPACES.has(requestedNamespace)
    ? requestedNamespace
    : FEISHU_SCOPED_NAMESPACE.test(requestedNamespace)
      ? 'feishu:base'
      : EMAIL_SCOPED_NAMESPACE.test(requestedNamespace)
        ? 'email:inbound'
        : '';
  return {
    id: isUuid(value.id) ? String(value.id) : '',
    status: projectedStatus,
    revision: safeInteger(value.revision),
    fileName:
      source === 'email'
        ? 'Inbound email preview'
        : boundedText(value.file_name ?? value.original_filename, 255),
    headers,
    mapping: mappingResult.mapping || {},
    source,
    sourceNamespace,
    sourceLabel:
      SAFE_SOURCE_NAMESPACES.get(sourceNamespace) ||
      (source === 'crm' ? 'Internal CRM' : source === 'manual' ? 'Manual import' : 'Import'),
    counts: normalizeCounts(
      value.counts ??
        value.summary ?? {
          total: value.total_count,
          processed: value.processed_count,
          ready: value.ready_count,
          conflict: value.conflict_count,
          invalid: value.invalid_count,
          skipped: value.skipped_count,
          imported: safeInteger(value.created_count) + safeInteger(value.merged_count),
          failed: value.failed_count
        }
    ),
    replayed: value.replayed === true,
    jobStatus: normalizedJobStatus,
    matchRunIds,
    errorCode: boundedText(value.error_code, 80),
    createdAt: safeDate(value.created_at),
    updatedAt: safeDate(value.updated_at),
    completedAt: safeDate(value.completed_at),
    records: recordsRaw.map(normalizeImportRecord).filter((record) => record.id)
  };
}

/** @param {unknown} raw */
export function normalizeImportRecordList(raw) {
  const items = Array.isArray(raw)
    ? raw
    : plainObject(raw) && Array.isArray(/** @type {Record<string, any>} */ (raw).results)
      ? /** @type {Record<string, any>} */ (raw).results
      : [];
  return items.map(normalizeImportRecord).filter((record) => record.id);
}

/** @param {{ status?: string, jobStatus?: string } | null | undefined} batch */
export function isImportBatchActive(batch) {
  if (TERMINAL_JOB_STATUSES.has(String(batch?.jobStatus || ''))) return false;
  return ACTIVE_STATUSES.has(String(batch?.status || ''));
}

/** @param {{ status?: string } | null | undefined} batch */
export function isImportBatchTerminal(batch) {
  return TERMINAL_STATUSES.has(String(batch?.status || ''));
}

/**
 * Encode a cell for a downloadable operator CSV. Prefix spreadsheet formulas
 * and quote according to RFC 4180.
 *
 * @param {unknown} value
 */
export function csvSafeCell(value) {
  let text = String(value ?? '');
  if (/^[\t\r ]*[=+\-@]/.test(text)) text = `'${text}`;
  if (/[",\r\n]/.test(text)) text = `"${text.replaceAll('"', '""')}"`;
  return text;
}
