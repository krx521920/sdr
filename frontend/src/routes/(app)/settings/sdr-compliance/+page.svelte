<script>
  import { PageHeader } from '$lib/components/layout';
  import {
    AlertTriangle,
    Ban,
    CheckCircle2,
    Clock3,
    DatabaseZap,
    FileSearch,
    Plus,
    RefreshCw,
    ShieldCheck,
    Trash2
  } from '@lucide/svelte';

  /** @type {{ data: any, form: any }} */
  let { data, form } = $props();

  const overview = $derived(data.overview || {});
  const settings = $derived(overview.settings || {});
  const summary = $derived(overview.summary || {});
  const choices = $derived(overview.choices || {});
  const channels = $derived(choices.channels || []);
  const lawfulBases = [
    ['unassessed', 'Not assessed'],
    ['consent', 'Consent'],
    ['legitimate_interest', 'Legitimate interest'],
    ['contract', 'Contract / pre-contract request'],
    ['legal_obligation', 'Legal obligation'],
    ['public_task', 'Public task'],
    ['vital_interest', 'Vital interest']
  ];

  const cards = $derived([
    { label: 'Provenance records', value: summary.provenance_records || 0, icon: FileSearch },
    { label: 'Unassessed basis', value: summary.unassessed || 0, icon: AlertTriangle },
    { label: 'Active DNC', value: summary.active_dnc || 0, icon: Ban },
    { label: 'Retention due', value: summary.retention_due || 0, icon: Clock3 },
    { label: 'Blocked decisions', value: summary.blocked_decisions || 0, icon: ShieldCheck }
  ]);

  /** @param {string | null | undefined} value */
  function displayDate(value) {
    if (!value) return '-';
    return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(
      new Date(value)
    );
  }
</script>

<svelte:head>
  <title>SDR Compliance - BottleCRM</title>
</svelte:head>

<PageHeader
  title="SDR Compliance & Governance"
  subtitle="Control lawful contact, country/channel DNC, provenance, retention, and deletion audit"
>
  {#snippet titleIcon()}<ShieldCheck class="size-4" />{/snippet}
</PageHeader>

<div class="space-y-6 p-6 md:p-8">
  {#if data.loadError || form?.actionError}
    <div class="flex gap-3 rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-800">
      <AlertTriangle class="mt-0.5 size-4 shrink-0" />
      <p>{form?.actionError || data.loadError}</p>
    </div>
  {/if}
  {#if form?.settingsSaved || form?.ruleSaved || form?.dncAdded || form?.provenanceSaved}
    <div
      class="flex gap-3 rounded-lg border border-emerald-200 bg-emerald-50 p-4 text-sm text-emerald-800"
    >
      <CheckCircle2 class="mt-0.5 size-4 shrink-0" />
      <p>Compliance controls were saved.</p>
    </div>
  {/if}

  <section class="grid gap-4 sm:grid-cols-2 xl:grid-cols-5">
    {#each cards as card (card.label)}
      <div
        class="rounded-xl border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-4"
      >
        <div class="flex items-center justify-between">
          <p class="text-xs font-medium text-[color:var(--text-muted)]">{card.label}</p>
          <card.icon class="size-4 text-violet-600" />
        </div>
        <p class="mt-3 text-2xl font-semibold">{card.value}</p>
      </div>
    {/each}
  </section>

  <section class="grid gap-6 xl:grid-cols-[1.1fr_0.9fr]">
    <div
      class="rounded-xl border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-5"
    >
      <div class="mb-5">
        <h2 class="font-semibold">Enforcement & retention</h2>
        <p class="mt-1 text-xs text-[color:var(--text-muted)]">
          Existing organizations stay in audit mode until enforcement is explicitly enabled.
        </p>
      </div>
      <form method="POST" action="?/saveSettings" class="space-y-5">
        <label
          class="flex items-start gap-3 rounded-lg border border-[color:var(--border-faint)] p-3"
        >
          <input
            type="checkbox"
            name="enforcement_enabled"
            checked={settings.enforcement_enabled}
          />
          <span>
            <span class="block text-sm font-medium">Enforce contact eligibility</span>
            <span class="text-xs text-[color:var(--text-muted)]"
              >Block sending when the lawful basis or permitted channel is missing.</span
            >
          </span>
        </label>
        <label
          class="flex items-start gap-3 rounded-lg border border-[color:var(--border-faint)] p-3"
        >
          <input
            type="checkbox"
            name="require_lawful_basis"
            checked={settings.require_lawful_basis}
          />
          <span>
            <span class="block text-sm font-medium">Require a lawful basis</span>
            <span class="text-xs text-[color:var(--text-muted)]"
              >An unassessed outbound record is blocked when enforcement is active.</span
            >
          </span>
        </label>
        <div class="grid gap-4 sm:grid-cols-3">
          <label class="text-xs font-medium"
            >Retention mode
            <select
              name="retention_mode"
              class="mt-1 w-full rounded-md border bg-transparent px-3 py-2 text-sm"
            >
              <option value="disabled" selected={settings.retention_mode === 'disabled'}
                >Disabled</option
              >
              <option value="audit_only" selected={settings.retention_mode === 'audit_only'}
                >Audit only</option
              >
              <option value="anonymize_sdr" selected={settings.retention_mode === 'anonymize_sdr'}
                >Anonymize SDR data</option
              >
            </select>
          </label>
          <label class="text-xs font-medium"
            >Retention days
            <input
              name="retention_days"
              type="number"
              min="30"
              max="3650"
              value={settings.retention_days || 730}
              class="mt-1 w-full rounded-md border bg-transparent px-3 py-2 text-sm"
            />
          </label>
          <label class="text-xs font-medium"
            >Deletion grace days
            <input
              name="deletion_grace_days"
              type="number"
              min="0"
              max="365"
              value={settings.deletion_grace_days ?? 30}
              class="mt-1 w-full rounded-md border bg-transparent px-3 py-2 text-sm"
            />
          </label>
        </div>
        <button
          class="rounded-md bg-violet-600 px-4 py-2 text-sm font-medium text-white hover:bg-violet-700"
          >Save controls</button
        >
      </form>
    </div>

    <div
      class="rounded-xl border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-5"
    >
      <div class="flex items-start gap-3">
        <DatabaseZap class="mt-0.5 size-5 text-amber-600" />
        <div>
          <h2 class="font-semibold">Retention scan</h2>
          <p class="mt-1 text-xs text-[color:var(--text-muted)]">
            Preview marks due records for review. Execute only anonymizes SDR-owned personal data;
            CRM records remain unchanged.
          </p>
        </div>
      </div>
      <form method="POST" action="?/scanRetention" class="mt-5 space-y-4">
        <label
          class="flex items-start gap-3 rounded-lg border border-amber-200 bg-amber-50 p-3 text-amber-900"
        >
          <input type="checkbox" name="execute" />
          <span class="text-xs"
            ><strong>Execute configured action.</strong> In anonymize mode this redacts eligible SDR records
            and cannot be reversed.</span
          >
        </label>
        <button
          class="inline-flex items-center gap-2 rounded-md border px-4 py-2 text-sm font-medium hover:bg-[color:var(--bg-muted)]"
        >
          <RefreshCw class="size-4" /> Run retention scan
        </button>
      </form>
      {#if form?.retentionResult}
        <div class="mt-4 rounded-lg bg-[color:var(--bg-muted)] p-3 text-xs">
          Due: {form.retentionResult.due} · Marked: {form.retentionResult.marked_due} · Anonymized: {form
            .retentionResult.anonymized} · CRM changed: {form.retentionResult.crm_records_changed}
        </div>
      {/if}
      <p class="mt-4 text-xs text-[color:var(--text-muted)]">
        Last scan: {displayDate(settings.last_retention_scan_at)}
      </p>
    </div>
  </section>

  <section
    class="rounded-xl border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-5"
  >
    <h2 class="font-semibold">Country / channel rules</h2>
    <p class="mt-1 text-xs text-[color:var(--text-muted)]">
      Use * as the global fallback. An exact country rule takes priority.
    </p>
    <form
      method="POST"
      action="?/saveRule"
      class="mt-4 grid gap-3 lg:grid-cols-[100px_150px_auto_auto_1fr_auto] lg:items-end"
    >
      <label class="text-xs font-medium"
        >Country<input
          name="country_code"
          value="*"
          maxlength="3"
          class="mt-1 w-full rounded-md border bg-transparent px-3 py-2 text-sm"
        /></label
      >
      <label class="text-xs font-medium"
        >Channel<select
          name="channel"
          class="mt-1 w-full rounded-md border bg-transparent px-3 py-2 text-sm"
          >{#each channels as channel}<option value={channel.value}>{channel.label}</option
            >{/each}</select
        ></label
      >
      <label class="flex items-center gap-2 pb-2 text-xs"
        ><input type="checkbox" name="is_allowed" checked /> Allowed</label
      >
      <label class="flex items-center gap-2 pb-2 text-xs"
        ><input type="checkbox" name="requires_consent" /> Consent required</label
      >
      <label class="text-xs font-medium"
        >Notes<input
          name="notes"
          class="mt-1 w-full rounded-md border bg-transparent px-3 py-2 text-sm"
        /></label
      >
      <button
        class="inline-flex items-center justify-center gap-2 rounded-md bg-violet-600 px-4 py-2 text-sm font-medium text-white"
        ><Plus class="size-4" /> Add rule</button
      >
    </form>
    <div class="mt-4 overflow-x-auto">
      <table class="w-full text-left text-sm">
        <thead class="border-b text-xs text-[color:var(--text-muted)]"
          ><tr
            ><th class="py-2">Country</th><th>Channel</th><th>Decision</th><th>Consent</th><th
              >Notes</th
            ><th></th></tr
          ></thead
        >
        <tbody>
          {#each data.rules?.results || [] as rule (rule.id)}
            <tr class="border-b border-[color:var(--border-faint)]"
              ><td class="py-3 font-mono">{rule.country_code}</td><td>{rule.channel}</td><td
                >{rule.is_allowed ? 'Allowed' : 'Blocked'}</td
              ><td>{rule.requires_consent ? 'Required' : 'No'}</td><td
                class="max-w-sm truncate text-xs text-[color:var(--text-muted)]"
                >{rule.notes || '-'}</td
              ><td class="text-right"
                ><form method="POST" action="?/deleteRule">
                  <input type="hidden" name="rule_id" value={rule.id} /><button
                    aria-label="Delete rule"
                    class="rounded p-2 text-red-600 hover:bg-red-50"
                    ><Trash2 class="size-4" /></button
                  >
                </form></td
              ></tr
            >
          {:else}<tr
              ><td colspan="6" class="py-5 text-center text-sm text-[color:var(--text-muted)]"
                >No country/channel rules.</td
              ></tr
            >{/each}
        </tbody>
      </table>
    </div>
  </section>

  <section
    class="rounded-xl border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-5"
  >
    <h2 class="font-semibold">Cross-channel do-not-contact</h2>
    <form
      method="POST"
      action="?/addDnc"
      class="mt-4 grid gap-3 lg:grid-cols-[150px_1fr_100px_190px_auto] lg:items-end"
    >
      <label class="text-xs font-medium"
        >Channel<select
          name="channel"
          class="mt-1 w-full rounded-md border bg-transparent px-3 py-2 text-sm"
          >{#each channels as channel}<option value={channel.value}>{channel.label}</option
            >{/each}</select
        ></label
      >
      <label class="text-xs font-medium"
        >Email, phone, or LinkedIn email/profile URL<input
          name="identifier"
          required
          class="mt-1 w-full rounded-md border bg-transparent px-3 py-2 text-sm"
        /></label
      >
      <label class="text-xs font-medium"
        >Country<input
          name="country_code"
          maxlength="3"
          class="mt-1 w-full rounded-md border bg-transparent px-3 py-2 text-sm"
        /></label
      >
      <label class="text-xs font-medium"
        >Reason<select
          name="reason"
          class="mt-1 w-full rounded-md border bg-transparent px-3 py-2 text-sm"
          >{#each choices.dnc_reasons || [] as reason}<option value={reason.value}
              >{reason.label}</option
            >{/each}</select
        ></label
      >
      <button
        class="inline-flex items-center justify-center gap-2 rounded-md bg-red-600 px-4 py-2 text-sm font-medium text-white"
        ><Ban class="size-4" /> Block contact</button
      >
    </form>
    <div class="mt-4 grid gap-3 md:grid-cols-2 xl:grid-cols-3">
      {#each data.dnc?.results || [] as entry (entry.id)}
        <div class="rounded-lg border border-[color:var(--border-faint)] p-3">
          <div class="flex items-start justify-between gap-3">
            <div>
              <p class="text-sm font-medium break-all">{entry.identifier}</p>
              <p class="mt-1 text-xs text-[color:var(--text-muted)]">
                {entry.channel} · {entry.reason} · {entry.country_code || 'global'}
              </p>
            </div>
            <form method="POST" action="?/releaseDnc">
              <input type="hidden" name="entry_id" value={entry.id} /><button
                class="rounded border px-2 py-1 text-xs hover:bg-[color:var(--bg-muted)]"
                >Release</button
              >
            </form>
          </div>
        </div>
      {:else}<p class="text-sm text-[color:var(--text-muted)]">No active DNC entries.</p>{/each}
    </div>
  </section>

  <section
    class="rounded-xl border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-5"
  >
    <h2 class="font-semibold">Data provenance & deletion requests</h2>
    <p class="mt-1 text-xs text-[color:var(--text-muted)]">
      Legal basis is an organization assessment, not a platform inference. Consult counsel for the
      countries and channels you use.
    </p>
    <div class="mt-4 space-y-4">
      {#each data.provenance?.results || [] as item (item.id)}
        <details class="rounded-lg border border-[color:var(--border-faint)] p-4">
          <summary class="cursor-pointer list-none">
            <div class="flex flex-wrap items-center justify-between gap-2">
              <div>
                <span class="font-medium">{item.contact_name || 'Unnamed contact'}</span><span
                  class="ml-2 text-sm text-[color:var(--text-muted)]"
                  >{item.company_name || item.intake_source}</span
                >
              </div>
              <div class="flex gap-2 text-xs">
                <span class="rounded-full bg-[color:var(--bg-muted)] px-2 py-1"
                  >{item.lawful_basis}</span
                ><span class="rounded-full bg-[color:var(--bg-muted)] px-2 py-1">{item.status}</span
                >
              </div>
            </div>
          </summary>
          <form
            method="POST"
            action="?/updateProvenance"
            class="mt-4 space-y-4 border-t border-[color:var(--border-faint)] pt-4"
          >
            <input type="hidden" name="intake_id" value={item.intake_id} />
            <div class="grid gap-3 md:grid-cols-2">
              <label class="text-xs font-medium"
                >Lawful basis<select
                  name="lawful_basis"
                  class="mt-1 w-full rounded-md border bg-transparent px-3 py-2 text-sm"
                  >{#each lawfulBases as basis}<option
                      value={basis[0]}
                      selected={item.lawful_basis === basis[0]}>{basis[1]}</option
                    >{/each}</select
                ></label
              ><label class="text-xs font-medium"
                >Assessment notes<input
                  name="lawful_basis_notes"
                  value={item.lawful_basis_notes}
                  class="mt-1 w-full rounded-md border bg-transparent px-3 py-2 text-sm"
                /></label
              >
            </div>
            <div class="grid gap-3 md:grid-cols-2">
              <label class="text-xs font-medium"
                >Consent timestamp<input
                  name="consent_at"
                  type="datetime-local"
                  value={item.consent_at ? item.consent_at.slice(0, 16) : ''}
                  class="mt-1 w-full rounded-md border bg-transparent px-3 py-2 text-sm"
                /></label
              ><label class="text-xs font-medium"
                >Consent evidence<input
                  name="consent_evidence"
                  value={item.consent_evidence}
                  placeholder="Form submission ID, signed record, or URL"
                  class="mt-1 w-full rounded-md border bg-transparent px-3 py-2 text-sm"
                /></label
              >
            </div>
            <fieldset>
              <legend class="text-xs font-medium">Permitted channels</legend>
              <div class="mt-2 flex flex-wrap gap-4">
                {#each channels as channel}<label class="flex items-center gap-2 text-xs"
                    ><input
                      type="checkbox"
                      name="allowed_channels"
                      value={channel.value}
                      checked={item.allowed_channels?.includes(channel.value)}
                    />
                    {channel.label}</label
                  >{/each}
              </div>
            </fieldset>
            <div class="flex flex-wrap items-center gap-3">
              <button class="rounded-md bg-violet-600 px-3 py-2 text-xs font-medium text-white"
                >Save assessment</button
              ><span class="text-xs text-[color:var(--text-muted)]"
                >Collected via {item.collection_method} · retain until {displayDate(
                  item.retention_until
                )}</span
              >
            </div>
          </form>
          <div class="mt-4 flex flex-wrap gap-2 border-t border-[color:var(--border-faint)] pt-4">
            {#if item.status === 'deletion_requested'}
              <form method="POST" action="?/deletion">
                <input type="hidden" name="intake_id" value={item.intake_id} /><input
                  type="hidden"
                  name="deletion_action"
                  value="cancel"
                /><button class="rounded-md border px-3 py-2 text-xs"
                  >Cancel deletion request</button
                >
              </form>
              <form method="POST" action="?/deletion" class="flex gap-2">
                <input type="hidden" name="intake_id" value={item.intake_id} /><input
                  type="hidden"
                  name="deletion_action"
                  value="anonymize"
                /><input
                  name="confirm_intake_id"
                  placeholder="Paste intake ID to confirm"
                  class="w-64 rounded-md border bg-transparent px-3 py-2 text-xs"
                /><button class="rounded-md bg-red-600 px-3 py-2 text-xs font-medium text-white"
                  >Anonymize SDR data</button
                >
              </form>
            {:else if item.status !== 'anonymized'}
              <form method="POST" action="?/deletion">
                <input type="hidden" name="intake_id" value={item.intake_id} /><input
                  type="hidden"
                  name="deletion_action"
                  value="request"
                /><button
                  class="rounded-md border border-amber-300 px-3 py-2 text-xs text-amber-800"
                  >Record deletion request</button
                >
              </form>
            {/if}
          </div>
        </details>
      {:else}<p class="py-6 text-center text-sm text-[color:var(--text-muted)]">
          No provenance records yet.
        </p>{/each}
    </div>
  </section>
</div>
