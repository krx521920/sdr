<script>
  import { enhance } from '$app/forms';
  import { goto } from '$app/navigation';
  import { resolve } from '$app/paths';
  import { page } from '$app/stores';
  import { toast } from 'svelte-sonner';
  import {
    AlertTriangle,
    ArrowLeft,
    Ban,
    BrainCircuit,
    Check,
    Clock3,
    DatabaseZap,
    Download,
    Eye,
    FileCheck2,
    Loader2,
    RefreshCw,
    Trash2,
    UserRoundCheck
  } from '@lucide/svelte';

  import { FilterPill, FilterStrip, PageHeader } from '$lib/components/layout';
  import * as AlertDialog from '$lib/components/ui/alert-dialog/index.js';
  import { Badge } from '$lib/components/ui/badge/index.js';
  import { Button } from '$lib/components/ui/button/index.js';
  import { SearchInput, SelectFilter } from '$lib/components/ui/filter/index.js';
  import * as Sheet from '$lib/components/ui/sheet/index.js';
  import {
    CONTACT_INTENT_CHANNELS,
    CONTACT_INTENT_PURPOSES,
    CONTACT_INTENT_STATUSES,
    EVIDENCE_KINDS,
    EVIDENCE_REVIEW_REASON_CODES,
    EVIDENCE_SOURCES,
    GOVERNANCE_QUEUES
  } from '$lib/matching/governance.js';

  let { data, form } = $props();

  const labels = {
    pending_ai: 'AI pending',
    expiring: 'Expiring',
    expired: 'Expired',
    blocked: 'Blocked',
    deletion_requested: 'Deletion requested',
    pending: 'Pending review',
    confirmed: 'Confirmed',
    rejected: 'Rejected',
    active: 'Active',
    allowed: 'Allowed',
    unknown: 'Unknown',
    open: 'Open',
    conditional: 'Conditional',
    not_open: 'Not open',
    withdrawn: 'Withdrawn',
    objected: 'Objected',
    due: 'Retention due',
    held: 'Legal hold',
    requested: 'Requested',
    scheduled: 'Scheduled',
    completed: 'Completed',
    cancelled: 'Cancelled'
  };
  const queueOptions = GOVERNANCE_QUEUES.map((value) => ({ value, label: labels[value] }));
  const sourceOptions = EVIDENCE_SOURCES.map((value) => ({ value, label: label(value) }));
  const kindOptions = EVIDENCE_KINDS.map((value) => ({ value, label: label(value) }));
  const intentOptions = CONTACT_INTENT_STATUSES.map((value) => ({ value, label: label(value) }));
  const intentChannelOptions = CONTACT_INTENT_CHANNELS.map((value) => ({
    value,
    label: label(value)
  }));
  const intentPurposeOptions = CONTACT_INTENT_PURPOSES.map((value) => ({
    value,
    label: label(value)
  }));
  const reviewReasonOptions = EVIDENCE_REVIEW_REASON_CODES.map((value) => ({
    value,
    label: label(value)
  }));

  let actionError = $state('');
  let busyAction = $state('');
  let permissionRevoked = $state(false);
  let reviewDialogOpen = $state(false);
  let reviewEvidence = $state(/** @type {any} */ (null));
  let reviewDecision = $state('confirm');
  let reviewReasonCode = $state('confirmed_accurate');
  let reviewKey = $state('');
  let intentDialogOpen = $state(false);
  let intentKey = $state('');
  let intentState = $state('unknown');
  let intentChannel = $state('email');
  let intentPurpose = $state('general_contact');
  let deletionDialogOpen = $state(false);
  let deletionAction = $state('request');
  let deletionConfirmation = $state('');
  let deletionKey = $state('');
  let retentionDialogOpen = $state(false);
  let retentionKey = $state('');
  let exportKey = $state('');
  let exportPersonKey = $state('');
  let liveMessage = $state('');

  const canManage = $derived(data.permissions?.manage === true && !permissionRevoked);
  const canExport = $derived(data.permissions?.export === true && !permissionRevoked);
  const canDelete = $derived(data.permissions?.delete === true && !permissionRevoked);
  const canRetention = $derived(data.permissions?.retention === true && !permissionRevoked);
  const editedIntent = $derived(
    data.intents.find(
      (intent) => intent.channel === intentChannel && intent.purpose === intentPurpose
    ) || null
  );
  const activeFilterCount = $derived(
    [data.filters.q, data.filters.queue, data.filters.source, data.filters.kind].filter(Boolean)
      .length
  );
  const summaryCards = $derived([
    { label: 'People', value: data.summary.total, icon: UserRoundCheck, queue: '' },
    { label: 'AI pending', value: data.summary.pendingAi, icon: BrainCircuit, queue: 'pending_ai' },
    { label: 'Expiring', value: data.summary.expiring, icon: Clock3, queue: 'expiring' },
    { label: 'Expired', value: data.summary.expired, icon: AlertTriangle, queue: 'expired' },
    { label: 'Blocked', value: data.summary.blocked, icon: Ban, queue: 'blocked' },
    {
      label: 'Deletion requested',
      value: data.summary.deletionRequested,
      icon: Trash2,
      queue: 'deletion_requested'
    }
  ]);

  $effect(() => {
    if (form?.actionError) actionError = String(form.actionError);
  });

  $effect(() => {
    const personId = data.selected?.person.id || '';
    if (personId && personId !== exportPersonKey) {
      exportPersonKey = personId;
      exportKey = globalThis.crypto?.randomUUID?.() || '';
    }
  });

  /** @param {string} value */
  function label(value) {
    if (labels[value]) return labels[value];
    return String(value || '')
      .replaceAll('_', ' ')
      .replace(/\b\w/g, (character) => character.toUpperCase());
  }

  /** @param {string} value */
  function formatDate(value) {
    if (!value) return '—';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return '—';
    return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(
      date
    );
  }

  /** @param {string} value */
  function datetimeLocal(value) {
    const date = value ? new Date(value) : new Date();
    if (Number.isNaN(date.getTime())) return '';
    const local = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
    return local.toISOString().slice(0, 16);
  }

  /** @param {Record<string, string | null>} changes */
  function updateQuery(changes) {
    const url = new URL($page.url);
    for (const [key, value] of Object.entries(changes)) {
      if (!value || value === 'ALL') url.searchParams.delete(key);
      else url.searchParams.set(key, value);
    }
    // The URL already contains any configured base path.
    // eslint-disable-next-line svelte/no-navigation-without-resolve
    return goto(url, { keepFocus: true, noScroll: true });
  }

  function clearFilters() {
    updateQuery({ q: null, queue: null, source: null, kind: null, person: null });
  }

  /** @param {any} person */
  function openPerson(person) {
    updateQuery({ person: person.person.id });
  }

  function closePerson() {
    updateQuery({ person: null });
  }

  /** @param {any} evidence @param {'confirm'|'reject'} decision */
  function openReview(evidence, decision) {
    if (!canManage || busyAction) return;
    reviewEvidence = evidence;
    reviewDecision = decision;
    reviewReasonCode = decision === 'confirm' ? 'confirmed_accurate' : 'insufficient_support';
    reviewKey = globalThis.crypto?.randomUUID?.() || '';
    actionError = '';
    reviewDialogOpen = true;
  }

  function openIntent() {
    if (!canManage || !data.selected || busyAction) return;
    const current = data.intents[0] || data.selected.intent;
    intentState = current.state || 'unknown';
    intentChannel = current.channel === 'other' && !current.id ? 'email' : current.channel;
    intentPurpose = current.purpose || 'general_contact';
    intentKey = globalThis.crypto?.randomUUID?.() || '';
    actionError = '';
    intentDialogOpen = true;
  }

  /** @param {'request'|'cancel'|'anonymize'} action */
  function openDeletion(action) {
    if (!canDelete || !data.selected || busyAction) return;
    deletionAction = action;
    deletionConfirmation = '';
    deletionKey = globalThis.crypto?.randomUUID?.() || '';
    actionError = '';
    deletionDialogOpen = true;
  }

  function openRetention() {
    if (!canRetention || busyAction) return;
    retentionKey = globalThis.crypto?.randomUUID?.() || '';
    actionError = '';
    retentionDialogOpen = true;
  }

  /** @param {string} action @param {string} successMessage @param {() => void} [onSuccess] */
  function enhanceAction(action, successMessage, onSuccess) {
    return ({ cancel }) => {
      if (busyAction) {
        cancel();
        return;
      }
      busyAction = action;
      actionError = '';
      liveMessage = `${action} in progress.`;
      return async ({ result, update }) => {
        busyAction = '';
        const actionData = /** @type {any} */ (result).data;
        if (result.type === 'success' && actionData?.governanceUpdated) {
          const runCount = Array.isArray(actionData.matchRunIds)
            ? actionData.matchRunIds.length
            : 0;
          const message = runCount
            ? `${successMessage} ${runCount} focused recomputation${runCount === 1 ? '' : 's'} queued.`
            : successMessage;
          liveMessage = message;
          toast.success(message);
          onSuccess?.();
          await update({ reset: false, invalidateAll: true });
          return;
        }
        if (result.type === 'failure' && result.status === 403) {
          permissionRevoked = true;
          reviewDialogOpen = false;
          intentDialogOpen = false;
          deletionDialogOpen = false;
          retentionDialogOpen = false;
        }
        await update({ reset: false, invalidateAll: false });
        actionError = actionData?.actionError || 'The governance action could not be completed.';
        liveMessage = actionError;
        toast.error(actionError);
      };
    };
  }

  const reviewEnhance = enhanceAction('Evidence review', 'Evidence review saved.', () => {
    reviewDialogOpen = false;
  });
  const intentEnhance = enhanceAction('Intent update', 'Contact intent recorded.', () => {
    intentDialogOpen = false;
  });
  const deletionEnhance = enhanceAction('Deletion update', 'Deletion status updated.', () => {
    deletionDialogOpen = false;
  });
  const retentionEnhance = enhanceAction('Retention scan', 'Retention scan completed.', () => {
    retentionDialogOpen = false;
  });

  /** @param {string} status */
  function statusClass(status) {
    if (
      [
        'blocked',
        'expired',
        'rejected',
        'anonymized',
        'objected',
        'withdrawn',
        'not_open'
      ].includes(status)
    ) {
      return 'border-[color:var(--red)] text-[color:var(--red-soft-text)]';
    }
    if (['pending', 'pending_ai', 'expiring', 'due', 'requested'].includes(status)) {
      return 'border-[color:var(--amber)] text-[color:var(--amber-soft-text)]';
    }
    if (['confirmed', 'active', 'allowed', 'open'].includes(status)) {
      return 'border-[color:var(--green)] text-[color:var(--green-soft-text)]';
    }
    return '';
  }
</script>

<svelte:head><title>Evidence governance · Matching</title></svelte:head>

<div class="flex min-h-0 flex-1 flex-col">
  <PageHeader
    title="Evidence governance"
    subtitle="Review evidence quality, stated intent, contact restrictions and data lifecycle"
    breadcrumb={[{ label: 'Matching', href: '/matching' }, { label: 'Governance' }]}
  >
    {#snippet actions()}
      <Button href="/matching" size="sm" variant="outline">
        <ArrowLeft class="size-3.5" />Back to matching
      </Button>
      {#if permissionRevoked}
        <Badge variant="outline">Access changed</Badge>
      {:else if canRetention}
        <Button size="sm" variant="outline" disabled={Boolean(busyAction)} onclick={openRetention}>
          <DatabaseZap class="size-3.5" />Retention scan
        </Button>
      {/if}
    {/snippet}
  </PageHeader>

  <FilterStrip>
    <SearchInput
      value={data.filters.q}
      placeholder="Search people…"
      onchange={(value) => updateQuery({ q: value, person: null })}
      class="w-full sm:w-56"
    />
    <SelectFilter
      options={queueOptions}
      value={data.filters.queue || 'ALL'}
      allLabel="All governance queues"
      onchange={(value) => updateQuery({ queue: String(value), person: null })}
    />
    <SelectFilter
      options={sourceOptions}
      value={data.filters.source || 'ALL'}
      allLabel="All evidence sources"
      onchange={(value) => updateQuery({ source: String(value), person: null })}
    />
    <SelectFilter
      options={kindOptions}
      value={data.filters.kind || 'ALL'}
      allLabel="All evidence kinds"
      onchange={(value) => updateQuery({ kind: String(value), person: null })}
    />
    {#if activeFilterCount > 0}
      <FilterPill label="Clear all" dashed onclick={clearFilters} />
    {/if}
    {#snippet meta()}<span>{data.personCount} people</span>{/snippet}
  </FilterStrip>

  <p class="sr-only" role="status" aria-live="polite">{liveMessage}</p>

  <main class="flex-1 space-y-5 px-5 py-5 md:px-8">
    {#if actionError}
      <div
        role="alert"
        class="rounded-lg border border-[color:var(--red)] bg-[color:var(--red-soft)] px-4 py-3 text-sm text-[color:var(--red-soft-text)]"
      >
        {actionError}
      </div>
    {/if}

    {#if form?.retentionResult}
      <div
        role="status"
        class="rounded-lg border border-[color:var(--green)] bg-[color:var(--green-soft)] px-4 py-3 text-sm text-[color:var(--green-soft-text)]"
      >
        Retention {form.retentionResult.execute ? 'execution' : 'preview'}: {form.retentionResult
          .due}
        due · {form.retentionResult.restricted} restricted · {form.retentionResult.expired} expired ·
        {form.retentionResult.anonymized} anonymized · {form.retentionResult.recomputed} recomputed.
      </div>
    {/if}

    <section aria-label="Governance summary" class="grid grid-cols-2 gap-3 lg:grid-cols-6">
      {#each summaryCards as card (card.label)}
        <button
          type="button"
          aria-pressed={(data.filters.queue || '') === card.queue}
          class="rounded-xl border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-4 text-left transition hover:border-[color:var(--border)] focus-visible:ring-2 focus-visible:ring-[color:var(--ring)] focus-visible:outline-none"
          onclick={() => updateQuery({ queue: card.queue || null, person: null })}
        >
          <span class="flex items-center justify-between gap-2">
            <span class="text-xs font-medium text-[color:var(--text-muted)]">{card.label}</span>
            <card.icon class="size-4 text-[color:var(--violet-soft-text)]" aria-hidden="true" />
          </span>
          <span class="mt-3 block text-2xl font-semibold text-[color:var(--text)] tabular-nums"
            >{card.value}</span
          >
        </button>
      {/each}
    </section>

    <aside
      class="rounded-xl border border-[color:var(--violet)] bg-[color:var(--violet-soft)] px-4 py-3 text-sm text-[color:var(--violet-soft-text)]"
      aria-label="Evidence governance privacy notice"
    >
      AI-generated evidence is a review lead, not a confirmed fact. It is excluded from ranking and
      outreach until a person confirms it. Raw identities, provider records and legal notes are not
      shown here.
    </aside>

    <section
      class="overflow-hidden rounded-xl border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)]"
      aria-labelledby="governance-people-title"
    >
      <div class="border-b border-[color:var(--border-faint)] px-5 py-4">
        <h2 id="governance-people-title" class="font-semibold text-[color:var(--text)]">
          People requiring governance review
        </h2>
        <p class="mt-1 text-xs text-[color:var(--text-subtle)]">
          Select a person to inspect safe evidence summaries, explicit intent and action blocks.
        </p>
      </div>

      {#if data.people.length === 0}
        <div class="px-5 py-14 text-center">
          <FileCheck2 class="mx-auto size-8 text-[color:var(--text-subtle)]" aria-hidden="true" />
          <p class="mt-3 text-sm font-medium text-[color:var(--text)]">No people in this queue</p>
          <p class="mt-1 text-xs text-[color:var(--text-subtle)]">
            Clear filters or choose another governance status.
          </p>
        </div>
      {:else}
        <div class="hidden overflow-x-auto md:block">
          <table class="w-full text-left text-sm">
            <thead class="bg-[color:var(--bg-subtle)] text-xs text-[color:var(--text-muted)]">
              <tr>
                <th class="px-5 py-3 font-medium">Person</th>
                <th class="px-4 py-3 font-medium">Evidence</th>
                <th class="px-4 py-3 font-medium">Intent</th>
                <th class="px-4 py-3 font-medium">Compliance</th>
                <th class="px-4 py-3 font-medium">Retention</th>
                <th class="px-4 py-3"><span class="sr-only">Open</span></th>
              </tr>
            </thead>
            <tbody>
              {#each data.people as item (item.person.id)}
                <tr class="border-t border-[color:var(--border-faint)] align-top">
                  <td class="px-5 py-4">
                    <p class="font-medium text-[color:var(--text)]">{item.person.displayName}</p>
                    <p class="mt-1 text-xs text-[color:var(--text-subtle)]">
                      {[item.person.currentTitle, item.person.currentCompany]
                        .filter(Boolean)
                        .join(' · ') || 'No role summary'}
                    </p>
                  </td>
                  <td class="px-4 py-4 text-xs text-[color:var(--text-muted)]">
                    <p>
                      {item.evidenceHealth.total} total · {item.evidenceHealth.pending} pending
                    </p>
                    {#if item.evidenceHealth.restricted}
                      <p class="mt-1 text-[color:var(--red-soft-text)]">
                        {item.evidenceHealth.restricted} restricted
                      </p>
                    {/if}
                    {#if item.evidenceHealth.expiring || item.evidenceHealth.expired}
                      <p class="mt-1 text-[color:var(--amber-soft-text)]">
                        {item.evidenceHealth.expiring} expiring · {item.evidenceHealth.expired} expired
                      </p>
                    {/if}
                  </td>
                  <td class="px-4 py-4">
                    <Badge variant="outline" class={statusClass(item.intent.state)}
                      >{label(item.intent.state)}</Badge
                    >
                  </td>
                  <td class="px-4 py-4">
                    <Badge variant="outline" class={statusClass(item.compliance.state)}
                      >{label(item.compliance.state)}</Badge
                    >
                  </td>
                  <td class="px-4 py-4 text-xs text-[color:var(--text-muted)]">
                    <p>{label(item.retention.status || 'unknown')}</p>
                    <p class="mt-1">{formatDate(item.retention.retentionUntil)}</p>
                  </td>
                  <td class="px-4 py-4 text-right">
                    <Button
                      type="button"
                      size="icon-sm"
                      variant="ghost"
                      aria-label="Review governance for {item.person.displayName}"
                      onclick={() => openPerson(item)}
                    >
                      <Eye class="size-4" />
                    </Button>
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>

        <ul class="divide-y divide-[color:var(--border-faint)] md:hidden">
          {#each data.people as item (item.person.id)}
            <li>
              <button
                type="button"
                class="w-full px-4 py-4 text-left focus-visible:ring-2 focus-visible:ring-[color:var(--ring)] focus-visible:outline-none"
                onclick={() => openPerson(item)}
              >
                <span class="flex items-start justify-between gap-3">
                  <span>
                    <span class="block font-medium text-[color:var(--text)]"
                      >{item.person.displayName}</span
                    >
                    <span class="mt-1 block text-xs text-[color:var(--text-subtle)]">
                      {item.evidenceHealth.total} total · {item.evidenceHealth.pending} pending
                    </span>
                  </span>
                  <Badge variant="outline" class={statusClass(item.compliance.state)}
                    >{label(item.compliance.state)}</Badge
                  >
                </span>
                <span class="mt-3 flex flex-wrap gap-2">
                  <Badge variant="neutral">Intent: {label(item.intent.state)}</Badge>
                  {#if item.evidenceHealth.expired > 0}
                    <Badge variant="outline" class={statusClass('expired')}
                      >{item.evidenceHealth.expired} expired</Badge
                    >
                  {/if}
                </span>
              </button>
            </li>
          {/each}
        </ul>
      {/if}
    </section>
  </main>
</div>

<Sheet.Root
  open={Boolean(data.selected)}
  onOpenChange={(open) => {
    if (!open && data.selected) closePerson();
  }}
>
  <Sheet.Content side="right" class="w-full gap-0 overflow-y-auto sm:max-w-2xl">
    {#if data.selected}
      <Sheet.Header class="border-b border-[color:var(--border-faint)] px-5 py-4">
        <Sheet.Title>{data.selected.person.displayName}</Sheet.Title>
        <Sheet.Description>
          Evidence health, explicit intent, compliance and Matching-owned data lifecycle.
        </Sheet.Description>
      </Sheet.Header>

      <div class="space-y-6 px-5 py-5">
        {#if data.selected.compliance.state === 'blocked'}
          <section
            role="alert"
            class="rounded-lg border border-[color:var(--red)] bg-[color:var(--red-soft)] p-4 text-sm text-[color:var(--red-soft-text)]"
          >
            <h3 class="flex items-center gap-2 font-semibold">
              <Ban class="size-4" />Contact blocked
            </h3>
            <p class="mt-2 text-xs">
              A contact block prevents outreach. Administrator exports remain separately
              permissioned and audited. A block does not mean this person is unsuitable for an
              opportunity.
            </p>
            {#if data.selected.compliance.reasons.length > 0}
              <ul class="mt-3 list-disc space-y-1 pl-5 text-xs">
                {#each data.selected.compliance.reasons as reason (`${reason.code}:${reason.label}`)}
                  <li>{reason.label}</li>
                {/each}
              </ul>
            {/if}
          </section>
        {/if}

        <section aria-labelledby="governance-evidence-title">
          <div class="flex items-center justify-between gap-3">
            <div>
              <h3 id="governance-evidence-title" class="font-semibold text-[color:var(--text)]">
                Evidence
              </h3>
              <p class="mt-1 text-xs text-[color:var(--text-subtle)]">
                Only bounded summaries and audit metadata are visible.
              </p>
            </div>
            <Badge variant="outline">{data.selected.evidence.length}</Badge>
          </div>
          {#if data.selected.evidence.length > 0}
            <ol class="mt-4 space-y-3">
              {#each data.selected.evidence as evidence (evidence.id)}
                <li
                  class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-4"
                >
                  <div class="flex flex-wrap items-center gap-2">
                    <Badge variant="neutral">{label(evidence.source)}</Badge>
                    <span class="text-xs text-[color:var(--text-subtle)]"
                      >{label(evidence.kind)}</span
                    >
                    {#if evidence.aiGenerated}
                      <Badge variant="outline" class={statusClass('pending')}>AI generated</Badge>
                    {/if}
                    <Badge variant="outline" class={statusClass(evidence.reviewStatus)}
                      >{label(evidence.reviewStatus)}</Badge
                    >
                    {#if evidence.freshness !== 'active'}
                      <Badge variant="outline" class={statusClass(evidence.freshness)}
                        >{label(evidence.freshness)}</Badge
                      >
                    {/if}
                  </div>
                  <p class="mt-3 text-sm leading-6 text-[color:var(--text-muted)]">
                    {evidence.summary || 'No safe evidence summary is available.'}
                  </p>
                  <dl
                    class="mt-3 grid gap-2 text-xs text-[color:var(--text-subtle)] sm:grid-cols-3"
                  >
                    <div>
                      <dt class="font-medium">Observed</dt>
                      <dd>{formatDate(evidence.observedAt)}</dd>
                    </div>
                    <div>
                      <dt class="font-medium">Valid until</dt>
                      <dd>{formatDate(evidence.validUntil)}</dd>
                    </div>
                    <div>
                      <dt class="font-medium">Confidence</dt>
                      <dd>{Math.round(evidence.confidence * 100)}%</dd>
                    </div>
                  </dl>
                  {#if canManage && evidence.reviewStatus === 'pending'}
                    <div
                      class="mt-4 flex flex-wrap gap-2 border-t border-[color:var(--border-faint)] pt-3"
                    >
                      <Button
                        type="button"
                        size="sm"
                        variant="outline"
                        disabled={Boolean(busyAction)}
                        onclick={() => openReview(evidence, 'confirm')}
                      >
                        <Check class="size-3.5" />Confirm
                      </Button>
                      <Button
                        type="button"
                        size="sm"
                        variant="destructive"
                        disabled={Boolean(busyAction)}
                        onclick={() => openReview(evidence, 'reject')}
                      >
                        Reject
                      </Button>
                    </div>
                  {/if}
                </li>
              {/each}
            </ol>
          {:else}
            <p
              class="mt-4 rounded-lg bg-[color:var(--bg-subtle)] p-4 text-sm text-[color:var(--text-muted)]"
            >
              No evidence summaries are available.
            </p>
          {/if}
        </section>

        <section
          class="border-t border-[color:var(--border-faint)] pt-5"
          aria-labelledby="intent-title"
        >
          <div class="flex flex-wrap items-start justify-between gap-3">
            <div>
              <h3 id="intent-title" class="font-semibold text-[color:var(--text)]">
                Explicit intent
              </h3>
              <p class="mt-1 text-xs text-[color:var(--text-subtle)]">
                Interest is context-specific and is not legal consent to contact. Compliance rules
                still apply.
              </p>
            </div>
            {#if canManage}
              <Button
                size="sm"
                variant="outline"
                disabled={Boolean(busyAction)}
                onclick={openIntent}
              >
                <UserRoundCheck class="size-3.5" />Record intent
              </Button>
            {/if}
          </div>
          <div class="mt-4 rounded-lg bg-[color:var(--bg-subtle)] p-4 text-sm">
            <div class="flex flex-wrap items-center gap-2">
              <Badge variant="outline" class={statusClass(data.selected.intent.state)}
                >{label(data.selected.intent.state)}</Badge
              >
              <Badge variant="neutral">{label(data.selected.intent.channel)}</Badge>
              <Badge variant="neutral">{label(data.selected.intent.purpose)}</Badge>
            </div>
            <p class="mt-2 text-xs text-[color:var(--text-subtle)]">
              Observed {formatDate(data.selected.intent.observedAt)} · valid until {formatDate(
                data.selected.intent.validUntil
              )}
            </p>
          </div>
          {#if data.intents.length > 1}
            <details class="mt-3 text-xs text-[color:var(--text-muted)]">
              <summary class="cursor-pointer font-medium">View intent history</summary>
              <ol class="mt-2 space-y-2 border-l border-[color:var(--border-faint)] pl-3">
                {#each data.intents as intent (intent.id)}
                  <li>
                    {label(intent.state)} · {label(intent.channel)} · {label(intent.purpose)} ·
                    {formatDate(intent.observedAt)}
                  </li>
                {/each}
              </ol>
            </details>
          {/if}
        </section>

        <section
          class="border-t border-[color:var(--border-faint)] pt-5"
          aria-labelledby="lifecycle-title"
        >
          <h3 id="lifecycle-title" class="font-semibold text-[color:var(--text)]">
            Data lifecycle
          </h3>
          <p class="mt-1 text-xs text-[color:var(--text-subtle)]">
            This area affects Matching-owned person and evidence data only. Linked CRM and SDR
            records follow their own retention policies.
          </p>
          <dl
            class="mt-4 grid gap-3 rounded-lg bg-[color:var(--bg-subtle)] p-4 text-sm sm:grid-cols-2"
          >
            <div>
              <dt class="text-xs text-[color:var(--text-subtle)]">Retention status</dt>
              <dd class="mt-1 font-medium">{label(data.selected.retention.status || 'unknown')}</dd>
            </div>
            <div>
              <dt class="text-xs text-[color:var(--text-subtle)]">Retain until</dt>
              <dd class="mt-1 font-medium">{formatDate(data.selected.retention.retentionUntil)}</dd>
            </div>
            <div>
              <dt class="text-xs text-[color:var(--text-subtle)]">Deletion</dt>
              <dd class="mt-1 font-medium">{label(data.selected.retention.deletionStatus)}</dd>
            </div>
          </dl>

          {#if canExport || canDelete}
            <div class="mt-4 flex flex-wrap gap-2">
              {#if canExport && !data.selected.compliance.blockedActions.includes('identity_export')}
                <form
                  method="POST"
                  action={resolve('/(app)/matching/governance/export/[personId]', {
                    personId: data.selected.person.id
                  })}
                >
                  <input type="hidden" name="expected_revision" value={data.selected.revision} />
                  <input type="hidden" name="idempotency_key" value={exportKey} />
                  <Button type="submit" size="sm" variant="outline">
                    <Download class="size-3.5" />Export Matching-owned JSON
                  </Button>
                </form>
              {/if}
              {#if canDelete && data.selected.retention.deletionStatus === 'requested'}
                <Button size="sm" variant="outline" onclick={() => openDeletion('cancel')}
                  >Cancel deletion request</Button
                >
                <Button size="sm" variant="destructive" onclick={() => openDeletion('anonymize')}>
                  <Trash2 class="size-3.5" />Anonymize data
                </Button>
              {:else if canDelete && data.selected.retention.deletionStatus !== 'completed'}
                <Button size="sm" variant="outline" onclick={() => openDeletion('request')}>
                  Request deletion
                </Button>
              {/if}
            </div>
          {/if}
          {#if canExport}
            <p class="mt-3 text-[11px] text-[color:var(--text-subtle)]">
              Exports may contain personal data. Each immediate export is audited and available only
              to organization administrators.
            </p>
          {/if}
        </section>
      </div>
    {/if}
  </Sheet.Content>
</Sheet.Root>

<AlertDialog.Root bind:open={reviewDialogOpen}>
  <AlertDialog.Content class="max-w-md">
    <AlertDialog.Header>
      <AlertDialog.Title
        >{reviewDecision === 'confirm'
          ? 'Confirm evidence?'
          : 'Reject evidence?'}</AlertDialog.Title
      >
      <AlertDialog.Description>
        AI-generated evidence is a review lead, not a confirmed fact. Choose a bounded audit reason;
        do not paste messages or provider output.
      </AlertDialog.Description>
    </AlertDialog.Header>
    <form method="POST" action="?/reviewEvidence" use:enhance={reviewEnhance} class="space-y-4">
      <input type="hidden" name="evidence_id" value={reviewEvidence?.id || ''} />
      <input type="hidden" name="decision" value={reviewDecision} />
      <input type="hidden" name="expected_revision" value={reviewEvidence?.revision ?? 0} />
      <input type="hidden" name="idempotency_key" value={reviewKey} />
      <label for="evidence-review-reason" class="block text-xs font-medium">
        Review reason
        <select
          id="evidence-review-reason"
          name="reason_code"
          bind:value={reviewReasonCode}
          required
          class="mt-1 w-full rounded-md border bg-transparent px-3 py-2 text-sm"
        >
          {#each reviewReasonOptions as option (option.value)}
            <option value={option.value}>{option.label}</option>
          {/each}
        </select>
      </label>
      <AlertDialog.Footer>
        <AlertDialog.Cancel disabled={Boolean(busyAction)}>Cancel</AlertDialog.Cancel>
        <Button
          type="submit"
          variant={reviewDecision === 'reject' ? 'destructive' : 'default'}
          disabled={Boolean(busyAction)}
          aria-busy={busyAction === 'Evidence review'}
        >
          {#if busyAction === 'Evidence review'}<Loader2 class="size-3.5 animate-spin" />{/if}
          {reviewDecision === 'confirm' ? 'Confirm evidence' : 'Reject evidence'}
        </Button>
      </AlertDialog.Footer>
    </form>
  </AlertDialog.Content>
</AlertDialog.Root>

<AlertDialog.Root bind:open={intentDialogOpen}>
  <AlertDialog.Content class="max-h-[90vh] max-w-lg overflow-y-auto">
    <AlertDialog.Header>
      <AlertDialog.Title>Record explicit intent</AlertDialog.Title>
      <AlertDialog.Description>
        Record only a directly stated or operator-confirmed status. Interest does not establish
        legal consent to contact.
      </AlertDialog.Description>
    </AlertDialog.Header>
    {#if data.selected}
      <form method="POST" action="?/saveIntent" use:enhance={intentEnhance} class="space-y-4">
        <input type="hidden" name="person_id" value={data.selected.person.id} />
        <input type="hidden" name="expected_revision" value={editedIntent?.revision ?? 0} />
        <input type="hidden" name="idempotency_key" value={intentKey} />
        <label for="intent-status" class="block text-xs font-medium">
          Status
          <select
            id="intent-status"
            name="state"
            bind:value={intentState}
            required
            class="mt-1 w-full rounded-md border bg-transparent px-3 py-2 text-sm"
          >
            {#each intentOptions as option (option.value)}
              <option value={option.value}>{option.label}</option>
            {/each}
          </select>
        </label>
        <div class="grid gap-3 sm:grid-cols-2">
          <label for="intent-channel" class="text-xs font-medium">
            Channel
            <select
              id="intent-channel"
              name="channel"
              bind:value={intentChannel}
              required
              class="mt-1 w-full rounded-md border bg-transparent px-3 py-2 text-sm"
            >
              {#each intentChannelOptions as option (option.value)}
                <option value={option.value}>{option.label}</option>
              {/each}
            </select>
          </label>
          <label for="intent-purpose" class="text-xs font-medium">
            Purpose
            <select
              id="intent-purpose"
              name="purpose"
              bind:value={intentPurpose}
              required
              class="mt-1 w-full rounded-md border bg-transparent px-3 py-2 text-sm"
            >
              {#each intentPurposeOptions as option (option.value)}
                <option value={option.value}>{option.label}</option>
              {/each}
            </select>
          </label>
        </div>
        <div class="grid gap-3 sm:grid-cols-2">
          <label for="intent-observed-at" class="text-xs font-medium">
            Observed at
            <input
              id="intent-observed-at"
              name="observed_at"
              type="datetime-local"
              required
              value={datetimeLocal(editedIntent?.observedAt || data.selected.intent.observedAt)}
              class="mt-1 w-full rounded-md border bg-transparent px-3 py-2 text-sm"
            />
          </label>
          <label for="intent-valid-until" class="text-xs font-medium">
            Valid until
            <input
              id="intent-valid-until"
              name="valid_until"
              type="datetime-local"
              value={editedIntent?.validUntil || data.selected.intent.validUntil
                ? datetimeLocal(editedIntent?.validUntil || data.selected.intent.validUntil)
                : ''}
              class="mt-1 w-full rounded-md border bg-transparent px-3 py-2 text-sm"
            />
          </label>
        </div>
        <label for="intent-reason" class="block text-xs font-medium">
          Audit basis
          <select
            id="intent-reason"
            name="reason_code"
            required
            class="mt-1 w-full rounded-md border bg-transparent px-3 py-2 text-sm"
          >
            <option value="person_stated">Person stated</option>
            <option value="operator_confirmed">Operator confirmed</option>
            <option value="administrative_update">Administrative update</option>
          </select>
        </label>
        <AlertDialog.Footer>
          <AlertDialog.Cancel disabled={Boolean(busyAction)}>Cancel</AlertDialog.Cancel>
          <Button
            type="submit"
            disabled={Boolean(busyAction)}
            aria-busy={busyAction === 'Intent update'}
          >
            {#if busyAction === 'Intent update'}<Loader2 class="size-3.5 animate-spin" />{/if}
            Record intent
          </Button>
        </AlertDialog.Footer>
      </form>
    {/if}
  </AlertDialog.Content>
</AlertDialog.Root>

<AlertDialog.Root bind:open={deletionDialogOpen}>
  <AlertDialog.Content class="max-w-md">
    <AlertDialog.Header>
      <AlertDialog.Title>
        {deletionAction === 'request'
          ? 'Request deletion?'
          : deletionAction === 'cancel'
            ? 'Cancel deletion request?'
            : 'Anonymize Matching-owned data?'}
      </AlertDialog.Title>
      <AlertDialog.Description>
        This affects Matching-owned person and evidence data only. Linked CRM and SDR records follow
        their own policies. Anonymization cannot be reversed.
      </AlertDialog.Description>
    </AlertDialog.Header>
    {#if data.selected}
      <form method="POST" action="?/deletion" use:enhance={deletionEnhance} class="space-y-4">
        <input type="hidden" name="person_id" value={data.selected.person.id} />
        <input type="hidden" name="deletion_action" value={deletionAction} />
        <input type="hidden" name="expected_revision" value={data.selected.retention.revision} />
        <input type="hidden" name="idempotency_key" value={deletionKey} />
        {#if deletionAction === 'anonymize'}
          <label for="deletion-confirm-person" class="block text-xs font-medium">
            Type the person ID to confirm
            <input
              id="deletion-confirm-person"
              name="confirm_person_id"
              bind:value={deletionConfirmation}
              autocomplete="off"
              class="mt-1 w-full rounded-md border bg-transparent px-3 py-2 font-mono text-xs"
            />
          </label>
        {/if}
        <AlertDialog.Footer>
          <AlertDialog.Cancel disabled={Boolean(busyAction)}>Keep data</AlertDialog.Cancel>
          <Button
            type="submit"
            variant={deletionAction === 'anonymize' ? 'destructive' : 'default'}
            disabled={Boolean(busyAction) ||
              (deletionAction === 'anonymize' && deletionConfirmation !== data.selected.person.id)}
            aria-busy={busyAction === 'Deletion update'}
          >
            {#if busyAction === 'Deletion update'}<Loader2 class="size-3.5 animate-spin" />{/if}
            {deletionAction === 'request'
              ? 'Request deletion'
              : deletionAction === 'cancel'
                ? 'Cancel request'
                : 'Anonymize data'}
          </Button>
        </AlertDialog.Footer>
      </form>
    {/if}
  </AlertDialog.Content>
</AlertDialog.Root>

<AlertDialog.Root bind:open={retentionDialogOpen}>
  <AlertDialog.Content class="max-w-md">
    <AlertDialog.Header>
      <AlertDialog.Title>Run Matching retention scan?</AlertDialog.Title>
      <AlertDialog.Description>
        Preview is safe. Execute applies the configured Matching-owned action and may anonymize
        eligible data. It never overrides deletion or do-not-contact restrictions.
      </AlertDialog.Description>
    </AlertDialog.Header>
    <form method="POST" action="?/retentionScan" use:enhance={retentionEnhance} class="space-y-4">
      <input type="hidden" name="expected_revision" value={data.summary.revision} />
      <input type="hidden" name="idempotency_key" value={retentionKey} />
      <label
        class="flex items-start gap-3 rounded-lg border border-[color:var(--amber)] p-3 text-xs"
      >
        <input type="checkbox" name="execute" />
        <span><strong>Execute configured action.</strong> Leave unchecked for a preview only.</span>
      </label>
      <AlertDialog.Footer>
        <AlertDialog.Cancel disabled={Boolean(busyAction)}>Cancel</AlertDialog.Cancel>
        <Button
          type="submit"
          disabled={Boolean(busyAction)}
          aria-busy={busyAction === 'Retention scan'}
        >
          {#if busyAction === 'Retention scan'}<Loader2 class="size-3.5 animate-spin" />{/if}
          <RefreshCw class="size-3.5" />Run scan
        </Button>
      </AlertDialog.Footer>
    </form>
  </AlertDialog.Content>
</AlertDialog.Root>
