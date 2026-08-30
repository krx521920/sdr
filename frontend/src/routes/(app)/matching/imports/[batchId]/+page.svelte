<script>
  import { enhance } from '$app/forms';
  import { invalidateAll } from '$app/navigation';
  import { resolve } from '$app/paths';
  import { toast } from 'svelte-sonner';
  import {
    AlertTriangle,
    ArrowLeft,
    CheckCircle2,
    Clock3,
    FileSpreadsheet,
    Link2,
    Loader2,
    RefreshCw,
    SkipForward,
    Users
  } from '@lucide/svelte';

  import { PageHeader } from '$lib/components/layout';
  import { Badge } from '$lib/components/ui/badge/index.js';
  import { Button } from '$lib/components/ui/button/index.js';
  import { Progress } from '$lib/components/ui/progress/index.js';
  import { StageStepper } from '$lib/components/ui/stage-stepper/index.js';
  import { isImportBatchActive, isImportBatchTerminal } from '$lib/matching/imports.js';

  let { data } = $props();

  const stages = [
    { value: 'upload', label: 'Upload', meta: 'CSV' },
    { value: 'mapping', label: 'Map', meta: 'Columns' },
    { value: 'preview', label: 'Preview', meta: 'Rows' },
    { value: 'commit', label: 'Commit', meta: 'Confirm' },
    { value: 'progress', label: 'Progress', meta: 'Import' },
    { value: 'review', label: 'Review', meta: 'Conflicts' }
  ];
  const statusFilters = [
    { value: '', label: 'All' },
    { value: 'ready', label: 'Ready' },
    { value: 'conflict', label: 'Conflicts' },
    { value: 'invalid', label: 'Invalid' },
    { value: 'created', label: 'Created' },
    { value: 'merged', label: 'Linked' },
    { value: 'skipped', label: 'Skipped' },
    { value: 'replayed', label: 'Replayed' },
    { value: 'failed', label: 'Failed' }
  ];

  let commitBusy = $state(false);
  let resolvingRecordId = $state('');
  let actionError = $state('');
  let pollingStopped = $state(false);
  let pollFailures = 0;

  const currentStage = $derived(
    ['queued', 'running'].includes(data.batch.status)
      ? 'progress'
      : ['completed', 'partial', 'failed'].includes(data.batch.status)
        ? 'review'
        : 'preview'
  );
  const progressValue = $derived(
    data.batch.counts.total > 0
      ? Math.min(100, Math.round((data.batch.counts.processed / data.batch.counts.total) * 100))
      : 0
  );
  const blockers = $derived(data.batch.counts.conflict + data.batch.counts.invalid);
  const canCommit = $derived(
    data.batch.status === 'previewed' &&
      data.batch.counts.ready > 0 &&
      data.batch.counts.conflict === 0 &&
      data.batch.counts.invalid === 0
  );
  const backHref = $derived(
    data.opportunity ? `/matching?opportunity=${encodeURIComponent(data.opportunity)}` : '/matching'
  );

  /** @param {string} status */
  function filterHref(status) {
    const path = resolve('/(app)/matching/imports/[batchId]', { batchId: data.batch.id });
    const queryParts = [];
    if (status) queryParts.push(`status=${encodeURIComponent(status)}`);
    if (data.opportunity) {
      queryParts.push(`opportunity=${encodeURIComponent(data.opportunity)}`);
    }
    const query = queryParts.join('&');
    return `${path}${query ? `?${query}` : ''}`;
  }

  /** @param {string} value */
  function formatDate(value) {
    if (!value) return '—';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return '—';
    return new Intl.DateTimeFormat(undefined, {
      dateStyle: 'medium',
      timeStyle: 'short'
    }).format(date);
  }

  /** @param {string} status */
  function statusLabel(status) {
    return (
      {
        previewed: 'Preview ready',
        queued: 'Queued',
        running: 'Importing',
        completed: 'Completed',
        partial: 'Completed with issues',
        failed: 'Failed'
      }[status] || 'Import'
    );
  }

  function commitEnhance({ formData, cancel }) {
    if (!canCommit || commitBusy) {
      cancel();
      return;
    }
    formData.set('idempotency_key', globalThis.crypto?.randomUUID?.() || '');
    commitBusy = true;
    actionError = '';
    return async ({ result, update }) => {
      commitBusy = false;
      const actionData = /** @type {any} */ (result).data;
      if (result.type === 'success' && actionData?.commitQueued) {
        toast.success('People import queued');
        await update({ reset: false, invalidateAll: true });
        return;
      }
      await update({ reset: false, invalidateAll: false });
      actionError = actionData?.actionError || 'The import could not be queued.';
      toast.error(actionError);
    };
  }

  /** @param {string} recordId */
  function resolveEnhance(recordId) {
    return ({ formData, cancel }) => {
      if (resolvingRecordId) {
        cancel();
        return;
      }
      formData.set('idempotency_key', globalThis.crypto?.randomUUID?.() || '');
      resolvingRecordId = recordId;
      actionError = '';
      return async ({ result, update }) => {
        resolvingRecordId = '';
        const actionData = /** @type {any} */ (result).data;
        if (result.type === 'success' && actionData?.conflictResolved) {
          toast.success('Conflict decision saved');
          await update({ reset: false, invalidateAll: true });
          return;
        }
        await update({ reset: false, invalidateAll: false });
        actionError = actionData?.actionError || 'The conflict could not be resolved.';
        toast.error(actionError);
      };
    };
  }

  async function refreshBatch() {
    actionError = '';
    try {
      await invalidateAll();
      pollFailures = 0;
      pollingStopped = false;
    } catch {
      actionError = 'The batch could not be refreshed. Your matching access may have changed.';
    }
  }

  $effect(() => {
    const activeKey = isImportBatchActive(data.batch) && !pollingStopped ? data.batch.id : '';
    if (!activeKey) return;
    const timer = setInterval(async () => {
      try {
        await invalidateAll();
        pollFailures = 0;
      } catch {
        pollFailures += 1;
        if (pollFailures >= 3) {
          pollingStopped = true;
          actionError =
            'Live updates stopped. Your matching access or connection may have changed.';
        }
      }
    }, 3000);
    return () => clearInterval(timer);
  });
</script>

<svelte:head><title>{data.batch.fileName || 'People import'} · Matching</title></svelte:head>

<div class="flex min-h-0 flex-1 flex-col">
  <PageHeader
    title={data.batch.fileName || 'People import'}
    subtitle="Safe row summaries, durable progress and explicit conflict decisions."
    breadcrumb={[{ label: 'Matching', href: backHref }, { label: 'CSV import' }]}
  >
    {#snippet actions()}
      <Button href={backHref} variant="outline" size="sm">
        <ArrowLeft class="size-3.5" />Back to matching
      </Button>
      <Button href="/matching/imports/new" variant="outline" size="sm">
        <FileSpreadsheet class="size-3.5" />New import
      </Button>
    {/snippet}
  </PageHeader>

  <main class="mx-auto w-full max-w-6xl flex-1 space-y-5 px-5 py-5 md:px-8">
    <div class="hidden sm:block"><StageStepper {stages} current={currentStage} /></div>
    <p class="text-xs font-medium text-[color:var(--text-muted)] sm:hidden">
      {currentStage === 'preview'
        ? 'Step 3 of 6 · Preview'
        : currentStage === 'progress'
          ? 'Step 5 of 6 · Progress'
          : 'Step 6 of 6 · Review'}
    </p>

    <section
      class="rounded-xl border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-5"
    >
      <div class="flex flex-wrap items-start justify-between gap-4">
        <div class="flex items-start gap-3">
          <div
            class="flex size-9 items-center justify-center rounded-lg bg-[color:var(--violet-soft)] text-[color:var(--violet-soft-text)]"
          >
            {#if isImportBatchActive(data.batch)}
              <Loader2 class="size-4 animate-spin motion-reduce:animate-none" aria-hidden="true" />
            {:else if data.batch.status === 'completed'}
              <CheckCircle2 class="size-4" aria-hidden="true" />
            {:else}
              <Clock3 class="size-4" aria-hidden="true" />
            {/if}
          </div>
          <div>
            <div class="flex flex-wrap items-center gap-2">
              <h2 class="text-base font-semibold text-[color:var(--text)]">
                {statusLabel(data.batch.status)}
              </h2>
              <Badge variant="outline">Revision {data.batch.revision}</Badge>
              <Badge variant="neutral">{data.batch.sourceLabel}</Badge>
              {#if data.batch.replayed}<Badge variant="neutral">Replayed</Badge>{/if}
            </div>
            <p class="mt-1 text-xs text-[color:var(--text-subtle)]">
              Created {formatDate(data.batch.createdAt)} · updated {formatDate(
                data.batch.updatedAt
              )}
            </p>
          </div>
        </div>
        <Button
          size="sm"
          variant="outline"
          onclick={refreshBatch}
          disabled={commitBusy || Boolean(resolvingRecordId)}
        >
          <RefreshCw class="size-3.5" />Refresh
        </Button>
      </div>

      {#if isImportBatchActive(data.batch)}
        <div class="mt-4" aria-live="polite">
          <div class="mb-1.5 flex justify-between text-xs text-[color:var(--text-muted)]">
            <span>{data.batch.counts.processed} of {data.batch.counts.total} processed</span>
            <span>{progressValue}%</span>
          </div>
          <Progress class="" value={progressValue} aria-label="Import progress" />
        </div>
      {/if}

      {#if data.batch.errorCode}
        <p
          class="mt-4 rounded-lg border border-[color:var(--red)] bg-[color:var(--red-soft)] px-3 py-2 text-sm text-[color:var(--red-soft-text)]"
          role="alert"
        >
          Import stopped with safe error code: <code>{data.batch.errorCode}</code>
        </p>
      {/if}
    </section>

    <section
      aria-label="Import counts"
      class="grid grid-cols-2 gap-3 sm:grid-cols-4 lg:grid-cols-7"
    >
      {#each [['Total', data.batch.counts.total], ['Ready', data.batch.counts.ready], ['Conflicts', data.batch.counts.conflict], ['Invalid', data.batch.counts.invalid], ['Imported', data.batch.counts.imported], ['Skipped', data.batch.counts.skipped], ['Failed', data.batch.counts.failed]] as item (item[0])}
        <div
          class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] px-3 py-3"
        >
          <p
            class="text-[11px] font-medium tracking-wide text-[color:var(--text-subtle)] uppercase"
          >
            {item[0]}
          </p>
          <p class="mt-1 text-xl font-semibold text-[color:var(--text)]">{item[1]}</p>
        </div>
      {/each}
    </section>

    {#if actionError}
      <div
        role="alert"
        class="rounded-lg border border-[color:var(--red)] bg-[color:var(--red-soft)] px-4 py-3 text-sm text-[color:var(--red-soft-text)]"
      >
        {actionError}
      </div>
    {/if}

    {#if data.batch.status === 'previewed'}
      <section
        class="rounded-xl border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-5"
      >
        <div class="flex flex-wrap items-center justify-between gap-4">
          <div>
            <h2 class="text-base font-semibold text-[color:var(--text)]">Commit this preview</h2>
            <p class="mt-1 text-sm text-[color:var(--text-muted)]">
              {#if blockers > 0}
                Resolve or skip {blockers} blocking row{blockers === 1 ? '' : 's'} before committing.
              {:else}
                The import will run in the background. This action is idempotent and explicitly
                confirmed.
              {/if}
            </p>
          </div>
          <form method="POST" action="?/commit" use:enhance={commitEnhance}>
            <input type="hidden" name="expected_revision" value={data.batch.revision} />
            <Button type="submit" disabled={!canCommit || commitBusy} aria-busy={commitBusy}>
              {#if commitBusy}<Loader2
                  class="size-3.5 animate-spin motion-reduce:animate-none"
                />{/if}
              {commitBusy ? 'Queueing…' : `Import ${data.batch.counts.ready} people`}
            </Button>
          </form>
        </div>
      </section>
    {/if}

    <section
      class="overflow-hidden rounded-xl border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)]"
    >
      <div class="border-b border-[color:var(--border-faint)] px-5 py-4">
        <div class="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h2 class="text-base font-semibold text-[color:var(--text)]">Batch records</h2>
            <p class="mt-1 text-xs text-[color:var(--text-subtle)]">
              Email identities show presence only; other identity values stay masked. Raw facts and
              source references are not shown.
            </p>
          </div>
          <span class="text-xs text-[color:var(--text-muted)]">{data.recordCount} records</span>
        </div>
        <nav aria-label="Record status" class="mt-3 flex gap-2 overflow-x-auto pb-1">
          {#each statusFilters as filter (filter.value)}
            <a
              href={resolve(/** @type {any} */ (filterHref(filter.value)))}
              aria-current={data.recordStatus === filter.value ? 'page' : undefined}
              class="shrink-0 rounded-full border px-3 py-1 text-xs font-medium focus-visible:ring-2 focus-visible:ring-[color:var(--ring)] focus-visible:outline-none {data.recordStatus ===
              filter.value
                ? 'border-[color:var(--text)] bg-[color:var(--text)] text-[color:var(--bg)]'
                : 'border-[color:var(--border-faint)] text-[color:var(--text-muted)] hover:border-[color:var(--border)]'}"
              >{filter.label}</a
            >
          {/each}
        </nav>
      </div>

      {#if data.records.length === 0}
        <div class="px-5 py-12 text-center">
          <Users class="mx-auto size-7 text-[color:var(--text-subtle)]" aria-hidden="true" />
          <p class="mt-2 text-sm font-medium text-[color:var(--text)]">No records in this view</p>
          <p class="mt-1 text-xs text-[color:var(--text-subtle)]">
            Choose another status or refresh the batch.
          </p>
        </div>
      {:else}
        <div class="hidden overflow-x-auto md:block">
          <table class="w-full text-left text-sm">
            <thead class="bg-[color:var(--bg-subtle)] text-xs text-[color:var(--text-muted)]">
              <tr>
                <th class="px-4 py-2.5 font-medium">Row</th>
                <th class="px-4 py-2.5 font-medium">Person</th>
                <th class="px-4 py-2.5 font-medium">Masked identity</th>
                <th class="px-4 py-2.5 font-medium">Evidence</th>
                <th class="px-4 py-2.5 font-medium">Status</th>
              </tr>
            </thead>
            <tbody>
              {#each data.records as record (record.id)}
                <tr class="border-t border-[color:var(--border-faint)] align-top">
                  <td class="px-4 py-3 font-mono text-xs text-[color:var(--text-subtle)]"
                    >{record.rowNumber}</td
                  >
                  <td class="px-4 py-3">
                    <p class="font-medium text-[color:var(--text)]">{record.person.displayName}</p>
                    <p class="text-xs text-[color:var(--text-subtle)]">
                      {record.person.currentTitle || record.person.currentCompany || '—'}
                    </p>
                  </td>
                  <td class="px-4 py-3 text-xs text-[color:var(--text-muted)]">
                    {record.identities
                      .map((identity) =>
                        identity.present
                          ? `${identity.kind}: present`
                          : `${identity.kind}: ${identity.maskedValue}`
                      )
                      .join(' · ') || '—'}
                  </td>
                  <td class="max-w-xs px-4 py-3 text-xs text-[color:var(--text-muted)]"
                    >{record.evidence.summary || '—'}</td
                  >
                  <td class="px-4 py-3"
                    ><Badge variant="outline">{record.status || 'unknown'}</Badge></td
                  >
                </tr>
                {#if record.errors.length > 0}
                  <tr class="border-t border-[color:var(--border-faint)]">
                    <td colspan="5" class="px-4 py-2 text-xs text-[color:var(--red)]">
                      {record.errors.map((entry) => entry.message).join(' · ')}
                    </td>
                  </tr>
                {/if}
              {/each}
            </tbody>
          </table>
        </div>

        <div class="divide-y divide-[color:var(--border-faint)] md:hidden">
          {#each data.records as record (record.id)}
            <article class="space-y-2 px-4 py-4">
              <div class="flex items-start justify-between gap-3">
                <div>
                  <p class="font-medium text-[color:var(--text)]">{record.person.displayName}</p>
                  <p class="text-xs text-[color:var(--text-subtle)]">CSV row {record.rowNumber}</p>
                </div>
                <Badge variant="outline">{record.status || 'unknown'}</Badge>
              </div>
              <p class="text-xs text-[color:var(--text-muted)]">
                {record.identities
                  .map((identity) =>
                    identity.present
                      ? `${identity.kind}: present`
                      : `${identity.kind}: ${identity.maskedValue}`
                  )
                  .join(' · ') || 'No identity summary'}
              </p>
              {#if record.evidence.summary}<p class="text-xs text-[color:var(--text-muted)]">
                  {record.evidence.summary}
                </p>{/if}
              {#if record.errors.length > 0}
                <p class="text-xs text-[color:var(--red)]">
                  {record.errors.map((entry) => entry.message).join(' · ')}
                </p>
              {/if}
            </article>
          {/each}
        </div>
      {/if}
    </section>

    {#if data.records.some((record) => record.status === 'conflict')}
      <section class="space-y-3" aria-labelledby="conflict-review-heading">
        <div>
          <h2 id="conflict-review-heading" class="text-base font-semibold text-[color:var(--text)]">
            Conflict review
          </h2>
          <p class="mt-1 text-sm text-[color:var(--text-muted)]">
            Linking adds this row's evidence to the selected existing person. It does not overwrite
            their profile or identity.
          </p>
        </div>
        {#each data.records.filter((record) => record.status === 'conflict') as record (record.id)}
          <article
            class="rounded-xl border border-[color:var(--amber)] bg-[color:var(--amber-soft)] p-4"
          >
            <div class="flex items-start gap-3">
              <AlertTriangle
                class="mt-0.5 size-4 shrink-0 text-[color:var(--amber-soft-text)]"
                aria-hidden="true"
              />
              <div class="min-w-0 flex-1">
                <h3 class="text-sm font-semibold text-[color:var(--text)]">
                  Row {record.rowNumber}: {record.person.displayName}
                </h3>
                <p class="mt-1 text-xs text-[color:var(--text-muted)]">
                  {record.conflict.code || 'An identity already belongs to another person.'}
                </p>
                <div class="mt-3 flex flex-wrap gap-2">
                  {#each record.conflict.candidates as candidate (candidate.id)}
                    <form method="POST" action="?/resolve" use:enhance={resolveEnhance(record.id)}>
                      <input type="hidden" name="record_id" value={record.id} />
                      <input type="hidden" name="resolution" value="link_existing" />
                      <input type="hidden" name="person_id" value={candidate.id} />
                      <input
                        type="hidden"
                        name="expected_revision"
                        value={record.conflict.revision}
                      />
                      <Button
                        type="submit"
                        size="sm"
                        variant="outline"
                        disabled={Boolean(resolvingRecordId)}
                      >
                        {#if resolvingRecordId === record.id}
                          <Loader2 class="size-3.5 animate-spin motion-reduce:animate-none" />
                        {:else}
                          <Link2 class="size-3.5" />
                        {/if}
                        Link to {candidate.displayName}
                      </Button>
                    </form>
                  {/each}
                  <form method="POST" action="?/resolve" use:enhance={resolveEnhance(record.id)}>
                    <input type="hidden" name="record_id" value={record.id} />
                    <input type="hidden" name="resolution" value="skip" />
                    <input
                      type="hidden"
                      name="expected_revision"
                      value={record.conflict.revision}
                    />
                    <Button
                      type="submit"
                      size="sm"
                      variant="outline"
                      disabled={Boolean(resolvingRecordId)}
                    >
                      <SkipForward class="size-3.5" />Skip row
                    </Button>
                  </form>
                </div>
              </div>
            </div>
          </article>
        {/each}
      </section>
    {/if}

    {#if isImportBatchTerminal(data.batch)}
      <section
        class="flex flex-wrap items-center justify-between gap-4 rounded-xl border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-5"
      >
        <div>
          <h2 class="text-base font-semibold text-[color:var(--text)]">
            Import processing finished
          </h2>
          <p class="mt-1 text-sm text-[color:var(--text-muted)]">
            {#if data.batch.matchRunIds.length > 0}
              Automatically queued {data.batch.matchRunIds.length} focused recomputation{data.batch
                .matchRunIds.length === 1
                ? ''
                : 's'} for affected people across open opportunities.
            {:else}
              No focused recomputation was needed because there are no open opportunities or no
              matching-relevant changes.
            {/if}
          </p>
        </div>
        <Button href={backHref}>Back to matching</Button>
      </section>
    {/if}
  </main>
</div>
