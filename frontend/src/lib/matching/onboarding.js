const PERSON_AVAILABILITY = new Set([
  'unknown',
  'available',
  'open_to_offers',
  'busy',
  'unavailable'
]);

const IDENTITY_KINDS = new Set(['email', 'phone', 'linkedin', 'whatsapp', 'wechat', 'external']);
const EVIDENCE_KINDS = new Set([
  'profile',
  'skill',
  'experience',
  'relationship',
  'interaction',
  'availability',
  'preference',
  'verification',
  'other'
]);

const MAX_TERMS = 50;
const MAX_TERM_LENGTH = 120;
const SENSITIVE_REFERENCE_KEYS = new Set([
  'access_token',
  'api_key',
  'apikey',
  'auth',
  'authorization',
  'code',
  'credential',
  'jwt',
  'password',
  'passwd',
  'secret',
  'session',
  'signature',
  'sig',
  'token'
]);

/** @typedef {{ payload?: any, error?: string, field?: string }} OnboardingBuildResult */

/** @param {unknown} value */
function text(value) {
  return typeof value === 'string' ? value.trim() : '';
}

/**
 * @param {unknown} value
 * @param {string} field
 * @returns {{ terms?: string[], error?: string, field?: string }}
 */
function parseTerms(value, field) {
  const raw = text(value);
  if (raw.length > 4000) {
    return { error: 'Keep each list within 4,000 characters.', field };
  }

  const terms = [];
  const seen = new Set();
  for (const part of raw.split(/[,;\n]/)) {
    const term = part.trim();
    if (!term) continue;
    if (term.length > MAX_TERM_LENGTH) {
      return { error: 'Each list value must be at most 120 characters.', field };
    }
    const key = term.toLowerCase();
    if (!seen.has(key)) {
      seen.add(key);
      terms.push(term);
    }
    if (terms.length > MAX_TERMS) {
      return { error: 'Use at most 50 values in each list.', field };
    }
  }
  return { terms };
}

/**
 * @param {unknown} value
 * @param {string} field
 * @param {boolean} [required]
 * @returns {{ value?: string, error?: string, field?: string }}
 */
function isoDate(value, field, required = false) {
  const raw = text(value);
  if (!raw) {
    return required ? { error: 'Enter a valid observation date and time.', field } : { value: '' };
  }
  const parsed = new Date(raw);
  if (Number.isNaN(parsed.getTime())) {
    return { error: 'Enter a valid date and time.', field };
  }
  return { value: parsed.toISOString() };
}

/**
 * Build the canonical Person part of an onboarding request. The backend owns
 * the active default and arbitrary attributes are intentionally unsupported.
 *
 * @param {Record<string, unknown>} input
 * @returns {OnboardingBuildResult}
 */
export function buildOnboardingPerson(input = {}) {
  const displayName = text(input.displayName);
  const currentTitle = text(input.currentTitle);
  const currentCompany = text(input.currentCompany);
  const location = text(input.location);
  const availability = text(input.availability).toLowerCase() || 'unknown';

  if (!displayName || displayName.length > 255) {
    return { error: 'Enter a person name of at most 255 characters.', field: 'displayName' };
  }
  for (const [value, field, label] of [
    [currentTitle, 'currentTitle', 'Current title'],
    [currentCompany, 'currentCompany', 'Current company'],
    [location, 'location', 'Location']
  ]) {
    if (value.length > 255) {
      return { error: `${label} must be at most 255 characters.`, field };
    }
  }
  if (!PERSON_AVAILABILITY.has(availability)) {
    return { error: 'Select a valid availability.', field: 'availability' };
  }

  const skills = parseTerms(input.skills, 'skills');
  if (skills.error) return skills;
  const roles = parseTerms(input.roles, 'roles');
  if (roles.error) return roles;

  return {
    payload: {
      display_name: displayName,
      current_title: currentTitle,
      current_company: currentCompany,
      location,
      availability,
      skills: skills.terms,
      roles: roles.terms
    }
  };
}

/**
 * Build one primary channel identity. The caller cannot select provenance:
 * manual onboarding always stores source=manual.
 *
 * @param {Record<string, unknown>} input
 * @returns {OnboardingBuildResult}
 */
export function buildOnboardingIdentity(input = {}) {
  const kind = text(input.kind).toLowerCase();
  let value = text(input.value);

  if (!IDENTITY_KINDS.has(kind)) {
    return { error: 'Select a valid identity type.', field: 'identityKind' };
  }
  if (!value || value.length > 500) {
    return { error: 'Enter an identity value of at most 500 characters.', field: 'identityValue' };
  }
  if (kind === 'email') {
    value = value.toLowerCase();
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value)) {
      return { error: 'Enter a valid email address.', field: 'identityValue' };
    }
  } else if (kind === 'phone' || kind === 'whatsapp') {
    value = value.replace(/[\s()-]/g, '');
    if (!/^\+?[0-9]{6,20}$/.test(value)) {
      return { error: 'Enter a valid phone number.', field: 'identityValue' };
    }
  }

  return {
    payload: {
      kind,
      normalized_value: value,
      source: 'manual',
      is_primary: true
    }
  };
}

/**
 * Build one append-only evidence record. Structured facts are deliberately
 * bounded to the four dimensions understood by the deterministic matcher.
 * Provenance is always manual for this UI, regardless of extra input keys.
 *
 * @param {Record<string, unknown>} input
 * @returns {OnboardingBuildResult}
 */
export function buildOnboardingEvidence(input = {}) {
  const kind = text(input.kind).toLowerCase();
  const summary = text(input.summary);
  const sourceUri = text(input.sourceUri);
  const confidence = Number(input.confidence);

  if (!EVIDENCE_KINDS.has(kind)) {
    return { error: 'Select a valid evidence type.', field: 'evidenceKind' };
  }
  if (!summary || summary.length > 5000) {
    return {
      error: 'Enter an evidence summary of at most 5,000 characters.',
      field: 'evidenceSummary'
    };
  }
  if (sourceUri.length > 1000) {
    return { error: 'Keep the reference link within 1,000 characters.', field: 'sourceUri' };
  }
  if (sourceUri) {
    try {
      const parsed = new URL(sourceUri);
      if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password) {
        throw new Error('unsafe reference');
      }
      if (parsed.hash) throw new Error('fragment reference');
      for (const rawKey of parsed.searchParams.keys()) {
        const key = rawKey.toLowerCase().replaceAll('-', '_');
        if (
          SENSITIVE_REFERENCE_KEYS.has(key) ||
          [...SENSITIVE_REFERENCE_KEYS].some((suffix) => key.endsWith(`_${suffix}`))
        ) {
          throw new Error('sensitive reference');
        }
      }
    } catch {
      return {
        error: 'Enter a safe HTTP or HTTPS reference link without credentials or tokens.',
        field: 'sourceUri'
      };
    }
  }
  if (!Number.isFinite(confidence) || confidence < 0 || confidence > 1) {
    return { error: 'Select a valid confidence level.', field: 'confidence' };
  }

  const observedAt = isoDate(input.observedAt, 'observedAt', true);
  if (observedAt.error) return observedAt;
  const validUntil = isoDate(input.validUntil, 'validUntil');
  if (validUntil.error) return validUntil;
  if (validUntil.value && new Date(validUntil.value) < new Date(observedAt.value)) {
    return { error: 'Valid until cannot precede observed at.', field: 'validUntil' };
  }

  /** @type {[string, unknown, string][]} */
  const factInputs = [
    ['skills', input.skills, 'evidenceSkills'],
    ['titles', input.titles, 'evidenceTitles'],
    ['locations', input.locations, 'evidenceLocations'],
    ['availability', input.availability, 'evidenceAvailability']
  ];
  const facts = {};
  for (const [dimension, raw, field] of factInputs) {
    const parsed = parseTerms(raw, field);
    if (parsed.error) return parsed;
    if (
      dimension === 'availability' &&
      parsed.terms.some((value) => !PERSON_AVAILABILITY.has(value))
    ) {
      return { error: 'Select a valid evidence availability.', field };
    }
    if (parsed.terms.length > 0) facts[dimension] = parsed.terms;
  }

  return {
    payload: {
      kind,
      source: 'manual',
      summary,
      facts,
      source_uri: sourceUri,
      observed_at: observedAt.value,
      valid_until: validUntil.value || null,
      confidence
    }
  };
}

/**
 * @param {{ person?: Record<string, unknown>, identity?: Record<string, unknown>, evidence?: Record<string, unknown> }} input
 * @returns {OnboardingBuildResult}
 */
export function buildPersonOnboardingPayload(input = {}) {
  const person = buildOnboardingPerson(input.person);
  if (!person.payload) return person;
  const identity = buildOnboardingIdentity(input.identity);
  if (!identity.payload) return identity;
  const evidence = buildOnboardingEvidence(input.evidence);
  if (!evidence.payload) return evidence;

  return {
    payload: {
      person: person.payload,
      identities: [identity.payload],
      evidence: [evidence.payload]
    }
  };
}

/**
 * Return a non-reversible presentation string for Review. The raw value remains
 * in the form state and request payload only.
 *
 * @param {unknown} kindValue
 * @param {unknown} identityValue
 */
export function maskIdentityValue(kindValue, identityValue) {
  const kind = text(kindValue).toLowerCase();
  const value = text(identityValue);
  if (!value) return 'Not entered';
  if (kind === 'email') {
    const at = value.lastIndexOf('@');
    if (at > 0) return `${value.slice(0, 1)}***${value.slice(at)}`;
  }
  const visible = value.replace(/\s/g, '').slice(-4);
  return visible ? `••••${visible}` : 'Entered';
}
