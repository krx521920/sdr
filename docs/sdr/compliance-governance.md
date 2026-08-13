# SDR compliance and data governance

This feature is an operational control layer, not a legal determination. Each organization remains responsible for choosing and documenting the appropriate legal basis, country rules, notices, and retention periods with qualified counsel.

## What is enforced

Every outbound contact decision is tenant-scoped and evaluated immediately before execution:

1. Validate and normalize the channel identifier.
2. Block an active channel-specific do-not-contact entry.
3. For email, also block the existing unsubscribe, complaint, bounce, and administrator suppression ledger.
4. Apply the exact country/channel rule, falling back to the `*` rule.
5. If the rule requires consent, require both a consent lawful basis and the channel in the provenance record.
6. When organization enforcement is enabled, require an assessed lawful basis and a permitted channel.

Email is checked at enrollment, scheduling, and sending. WhatsApp and LinkedIn are checked when creating the durable provider job and again before the provider API call. A block creates an idempotent audit event and an executor-specific skipped record; it does not silently discard the prospect.

Existing organizations default to:

- `enforcement_enabled = false`
- `require_lawful_basis = true`
- `retention_mode = audit_only`
- `retention_days = 730`
- `deletion_grace_days = 30`

Explicit DNC entries, email suppressions, and country/channel deny rules are enforced even while general lawful-basis enforcement is disabled.

## Provenance

Every intake has one `SDRDataProvenance` record containing:

- collection method and source URL;
- lawful basis and organization assessment notes;
- consent timestamp and evidence reference when consent is used;
- normalized country;
- explicitly permitted channels;
- retention deadline and deletion/anonymization state.

The migration backfills existing intakes as `unassessed`; it never invents consent or legitimate interest. CSV imports can provide `lawful_basis`, `lawful_basis_notes`, `consent_at`, `consent_evidence`, and `allowed_channels`. Apollo imports deliberately remain unassessed until the organization performs its own review.

## Retention and deletion

Celery Beat runs `sdr.scan_compliance_retention` daily at 02:15. Modes are:

- `disabled`: ignore ordinary retention deadlines, but continue processing confirmed deletion requests after their grace period;
- `audit_only`: mark due provenance records for administrator review;
- `anonymize_sdr`: anonymize due SDR-owned data when the scheduled scan executes.

The administrator UI can preview a scan or explicitly execute it. Anonymization redacts intake payloads, research content, nurture recipients/content, prospect contact data, WhatsApp recipients, LinkedIn recipients/message text, and external provider identifiers. Pending channel jobs become `skipped`.

CRM Lead data is intentionally not changed automatically because it may have a separate business or statutory retention basis. The compliance event records `crm_lead_review_required` when a linked CRM Lead exists. Deletion workflow owners must review that record and any downstream systems separately.

Minimal email suppression and DNC data is retained independently to avoid contacting someone who objected. Organizations should document the lawful basis and retention period for those suppression records.

An active deletion request immediately blocks further intake processing, contact
decisions, response delivery, and Feishu Base research export. The configured
grace period controls when SDR-owned data is anonymized; it does not permit new
processing or contact while the request is pending. Reconciliation and provider
workers recheck this state before recreating or executing queued work.

Provider-owned WhatsApp and LinkedIn audit records are redacted through data-governance adapters registered by `integrations`. This keeps the retention transaction complete without allowing the SDR module to import or query concrete provider models.

## API

- `GET /api/sdr/compliance/` — overview, choices, and recent events
- `GET|PATCH /api/sdr/compliance/settings/`
- `GET|POST /api/sdr/compliance/rules/`
- `PATCH|DELETE /api/sdr/compliance/rules/{id}/`
- `GET|POST /api/sdr/compliance/dnc/`
- `DELETE /api/sdr/compliance/dnc/{id}/` — release, never hard-delete
- `GET /api/sdr/compliance/provenance/`
- `PATCH /api/sdr/compliance/provenance/{intake_id}/`
- `POST /api/sdr/compliance/intakes/{intake_id}/deletion/`
- `POST /api/sdr/compliance/retention/scan/`

Immediate anonymization requires `confirm_intake_id` to exactly match the target intake. All endpoints require an authenticated organization administrator and filter by the active organization.

## Database isolation

Migration `sdr.0020_compliance_governance` creates and enables forced PostgreSQL RLS on:

- `sdr_compliance_settings`
- `sdr_channel_compliance_rule`
- `sdr_compliance_event`
- `sdr_data_provenance`
- `sdr_do_not_contact`

The tables are also registered in the central RLS catalog.

## Regulatory design references

The control model follows the GDPR principles and legal bases in Articles 5 and 6, the right to erasure in Article 17, and the unconditional right to object to processing for direct marketing in Article 21. See the official [EUR-Lex GDPR text](https://eur-lex.europa.eu/eli/reg/2016/679/oj/eng) and the European Data Protection Board's [lawful processing guide](https://www.edpb.europa.eu/sme/be-compliant/process-personal-data-lawfully_en).
