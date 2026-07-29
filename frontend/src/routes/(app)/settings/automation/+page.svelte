<script>
  import { enhance } from '$app/forms';
  import { toast } from 'svelte-sonner';
  import { Button } from '$lib/components/ui/button/index.js';
  import { Badge } from '$lib/components/ui/badge/index.js';
  import { SectionCard } from '$lib/components/ui/section-card/index.js';
  import { PageHeader } from '$lib/components/layout';
  import {
    AlertTriangle,
    CheckCircle2,
    Clock3,
    LoaderCircle,
    RefreshCw,
    RotateCcw
  } from '@lucide/svelte';

  /** @type {{ data: any, form: any }} */
  let { data, form } = $props();

  let retryingId = $state('');
  const summary = $derived(data.jobs?.summary || {});
  const activeCount = $derived(
    (summary.pending || 0) +
      (summary.queued || 0) +
      (summary.running || 0) +
      (summary.retry_scheduled || 0)
  );

  $effect(() => {
    if (data.loadError) toast.error(data.loadError);
    if (form?.actionError) toast.error(form.actionError);
    if (form?.retried) toast.success('Dead-letter job reopened for processing.');
  });

  /** @param {string} status */
  function statusLabel(status) {
    return (
      {
        pending: 'Pending',
        queued: 'Queued',
        running: 'Running',
        retry_scheduled: 'Retry scheduled',
        succeeded: 'Succeeded',
        dead_letter: 'Dead letter',
        cancelled: 'Cancelled'
      }[status] || status
    );
  }

  /** @param {string} status */
  function statusClass(status) {
    if (status === 'succeeded') {
      return 'border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-900 dark:bg-emerald-950 dark:text-emerald-400';
    }
    if (status === 'dead_letter') {
      return 'border-red-200 bg-red-50 text-red-700 dark:border-red-900 dark:bg-red-950 dark:text-red-400';
    }
    if (status === 'running' || status === 'queued') {
      return 'border-blue-200 bg-blue-50 text-blue-700 dark:border-blue-900 dark:bg-blue-950 dark:text-blue-400';
    }
    return 'border-amber-200 bg-amber-50 text-amber-700 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-400';
  }

  /** @param {string} name */
  function jobLabel(name) {
    if (name === 'facebook.process_lead') return 'Facebook lead intake';
    return name;
  }

  /** @param {string | null} value */
  function formatDate(value) {
    if (!value) return '—';
    return new Intl.DateTimeFormat(undefined, {
      dateStyle: 'medium',
      timeStyle: 'short'
    }).format(new Date(value));
  }
</script>

<svelte:head>
  <title>Automation Operations - BottleCRM</title>
</svelte:head>

<PageHeader
  title="Automation Operations"
  subtitle="Monitor durable lead-processing jobs, retries, and items that need attention"
/>

<div class="space-y-6 p-6 md:p-8">
  <div class="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
    <div
      class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-4"
    >
      <div class="flex items-center justify-between">
        <p class="text-xs font-medium text-[color:var(--text-muted)]">Active</p>
        <LoaderCircle class="size-4 text-blue-500" />
      </div>
      <p class="mt-2 text-2xl font-semibold text-[color:var(--text-primary)]">{activeCount}</p>
      <p class="mt-1 text-xs text-[color:var(--text-muted)]">
        Queued, running, or waiting to retry
      </p>
    </div>
    <div
      class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-4"
    >
      <div class="flex items-center justify-between">
        <p class="text-xs font-medium text-[color:var(--text-muted)]">Succeeded</p>
        <CheckCircle2 class="size-4 text-emerald-500" />
      </div>
      <p class="mt-2 text-2xl font-semibold text-[color:var(--text-primary)]">
        {summary.succeeded || 0}
      </p>
      <p class="mt-1 text-xs text-[color:var(--text-muted)]">Completed without manual action</p>
    </div>
    <div
      class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-4"
    >
      <div class="flex items-center justify-between">
        <p class="text-xs font-medium text-[color:var(--text-muted)]">Retry scheduled</p>
        <Clock3 class="size-4 text-amber-500" />
      </div>
      <p class="mt-2 text-2xl font-semibold text-[color:var(--text-primary)]">
        {summary.retry_scheduled || 0}
      </p>
      <p class="mt-1 text-xs text-[color:var(--text-muted)]">Protected by exponential backoff</p>
    </div>
    <div
      class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-4"
    >
      <div class="flex items-center justify-between">
        <p class="text-xs font-medium text-[color:var(--text-muted)]">Needs attention</p>
        <AlertTriangle class="size-4 text-red-500" />
      </div>
      <p class="mt-2 text-2xl font-semibold text-[color:var(--text-primary)]">
        {summary.dead_letter || 0}
      </p>
      <p class="mt-1 text-xs text-[color:var(--text-muted)]">
        Dead-letter jobs available for replay
      </p>
    </div>
  </div>

  <SectionCard>
    {#snippet title()}
      <div>
        <h3 class="text-[16px] font-medium text-[color:var(--text-primary)]">Job ledger</h3>
        <p class="text-[12px] text-[color:var(--text-muted)]">
          Every lead event is persisted before it reaches the worker queue.
        </p>
      </div>
    {/snippet}
    {#snippet actions()}
      <form method="GET" class="flex items-center gap-2">
        <select
          name="status"
          aria-label="Filter by status"
          class="h-8 rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] px-2 text-xs text-[color:var(--text-primary)]"
          value={data.selectedStatus}
        >
          <option value="">All statuses</option>
          <option value="pending">Pending</option>
          <option value="queued">Queued</option>
          <option value="running">Running</option>
          <option value="retry_scheduled">Retry scheduled</option>
          <option value="succeeded">Succeeded</option>
          <option value="dead_letter">Dead letter</option>
        </select>
        <Button type="submit" variant="outline" size="sm" class="gap-1.5"
          ><RefreshCw class="size-3.5" />Apply</Button
        >
      </form>
    {/snippet}

    {#if data.jobs.results.length}
      <div class="overflow-x-auto rounded-lg border border-[color:var(--border-faint)]">
        <table class="w-full min-w-[850px] text-left text-sm">
          <thead class="bg-[color:var(--bg-subtle)] text-xs text-[color:var(--text-muted)]">
            <tr>
              <th class="px-4 py-3 font-medium">Job</th>
              <th class="px-4 py-3 font-medium">Status</th>
              <th class="px-4 py-3 font-medium">Attempts</th>
              <th class="px-4 py-3 font-medium">Created</th>
              <th class="px-4 py-3 font-medium">Last error</th>
              <th class="px-4 py-3 text-right font-medium">Action</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-[color:var(--border-faint)]">
            {#each data.jobs.results as job (job.id)}
              <tr class="bg-[color:var(--bg-elevated)] align-top">
                <td class="px-4 py-3">
                  <p class="font-medium text-[color:var(--text-primary)]">{jobLabel(job.name)}</p>
                  <p class="mt-1 max-w-[240px] truncate text-xs text-[color:var(--text-muted)]">
                    {job.idempotency_key}
                  </p>
                </td>
                <td class="px-4 py-3"
                  ><Badge class={statusClass(job.status)}>{statusLabel(job.status)}</Badge></td
                >
                <td class="px-4 py-3 text-[color:var(--text-muted)]"
                  >{job.attempt_count} / {job.max_attempts}</td
                >
                <td class="px-4 py-3 text-xs text-[color:var(--text-muted)]"
                  >{formatDate(job.created_at)}</td
                >
                <td class="max-w-[280px] px-4 py-3">
                  {#if job.last_error_message}
                    <p class="line-clamp-2 text-xs text-red-600 dark:text-red-400">
                      {job.last_error_message}
                    </p>
                  {:else}
                    <span class="text-xs text-[color:var(--text-muted)]">—</span>
                  {/if}
                </td>
                <td class="px-4 py-3 text-right">
                  {#if job.status === 'dead_letter'}
                    <form
                      method="POST"
                      action="?/retry"
                      use:enhance={() => {
                        retryingId = job.id;
                        return async ({ update }) => {
                          retryingId = '';
                          await update({ reset: false, invalidateAll: true });
                        };
                      }}
                    >
                      <input type="hidden" name="job_id" value={job.id} />
                      <Button
                        type="submit"
                        variant="outline"
                        size="sm"
                        disabled={retryingId === job.id}
                        class="gap-1.5"
                      >
                        <RotateCcw class="size-3.5" />
                        {retryingId === job.id ? 'Reopening…' : 'Retry'}
                      </Button>
                    </form>
                  {/if}
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {:else}
      <div class="rounded-lg border border-dashed border-[color:var(--border)] p-10 text-center">
        <CheckCircle2 class="mx-auto size-7 text-emerald-500" />
        <p class="mt-3 text-sm font-medium text-[color:var(--text-primary)]">No matching jobs</p>
        <p class="mt-1 text-xs text-[color:var(--text-muted)]">
          New lead-processing events will appear here automatically.
        </p>
      </div>
    {/if}
  </SectionCard>
</div>
