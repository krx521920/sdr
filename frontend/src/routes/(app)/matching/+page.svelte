<script>
  import { enhance } from '$app/forms';
  import { goto, invalidateAll } from '$app/navigation';
  import { page } from '$app/stores';
  import { toast } from 'svelte-sonner';
  import {
    Activity,
    Building2,
    Check,
    ChevronRight,
    CircleAlert,
    Clock,
    Eye,
    MapPin,
    RefreshCw,
    ShieldCheck,
    Sparkles,
    Target,
    Users,
    X
  } from '@lucide/svelte';

  import { PageHeader, FilterStrip, FilterPill } from '$lib/components/layout';
  import * as AlertDialog from '$lib/components/ui/alert-dialog/index.js';
  import { Badge } from '$lib/components/ui/badge/index.js';
  import { Button } from '$lib/components/ui/button/index.js';
  import * as Dialog from '$lib/components/ui/dialog/index.js';
  import { SearchInput, SelectFilter } from '$lib/components/ui/filter';
  import { Progress } from '$lib/components/ui/progress/index.js';
  import * as Sheet from '$lib/components/ui/sheet/index.js';
  import { Textarea } from '$lib/components/ui/textarea/index.js';
  import {
    decisionTargetsForStatus,
    isMatchRunActive,
    isMatchRunSkipped,
    isMatchRunSuccessful,
    isMatchRunTerminal,
    scoreLabel
  } from '$lib/matching/workbench.js';

  let { data } = $props();

  const opportunityStatusOptions = [
    { value: 'draft', label: 'Draft' },
    { value: 'open', label: 'Open' },
    { value: 'paused', label: 'Paused' },
    { value: 'filled', label: 'Filled' },
    { value: 'closed', label: 'Closed' }
  ];
  const opportunityTypeOptions = [
    { value: 'customer', label: 'Customer' },
    { value: 'employment', label: 'Employment' },
    { value: 'contractor', label: 'Contractor' },
    { value: 'project', label: 'Project' },
    { value: 'expert', label: 'Expert' },
    { value: 'referral', label: 'Referral' },
    { value: 'partnership', label: 'Partnership' }
  ];
  const matchStatusOptions = [
    { value: 'proposed', label: 'Proposed' },
    { value: 'reviewing', label: 'Reviewing' },
    { value: 'shortlisted', label: 'Shortlisted' },
    { value: 'accepted', label: 'Accepted' },
    { value: 'rejected', label: 'Rejected' },
    { value: 'expired', label: 'Expired' }
  ];
  const decisionReasonCodes = {
    reviewing: 'needs_review',
    shortlisted: 'strong_fit',
    accepted: 'approved',
    rejected: 'not_a_fit'
  };

  let selectedMatchId = $state(/** @type {string | null} */ (null));
  let evidenceSheetOpen = $state(false);
  let recomputeDialogOpen = $state(false);
  let decisionDialogOpen = $state(false);
  let recomputing = $state(false);
  let decisionBusyId = $state(/** @type {string | null} */ (null));
  let pendingDecision = $state('');
  let pendingReasonCode = $state('');
  let decisionReason = $state('');
  let recomputeIdempotencyKey = $state('');
  let decisionIdempotencyKey = $state('');
  let liveMessage = $state('');
  let polledRun = $state(/** @type {any} */ (null));
  let polledRunHistory = $state(/** @type {any[]} */ ([]));
  let pollFailures = $state(0);
  let handledTerminalRunId = $state('');
  let permissionRevoked = $state(false);
  /** @type {ReturnType<typeof setInterval> | null} */
  let pollTimer = null;
  let pollInFlight = false;
  /** @type {HTMLFormElement} */
  let recomputeForm;

  const selectedMatch = $derived(
    data.matches.find((match) => match.id === selectedMatchId) || data.matches[0] || null
  );
  const currentRun = $derived(
    polledRun?.opportunityId === data.selectedOpportunity?.id ? polledRun : data.currentRun
  );
  const runHistory = $derived(
    polledRunHistory.length > 0 ? polledRunHistory : data.runHistory || []
  );
  const runActive = $derived(isMatchRunActive(currentRun));
  const canManage = $derived(data.permissions?.manage === true && !permissionRevoked);
  const canRecompute = $derived(data.permissions?.recompute === true && !permissionRevoked);
  const canDecide = $derived(data.permissions?.decide === true && !permissionRevoked);
  const isReadOnly = $derived(!canManage && !canRecompute && !canDecide);
  const recomputeBusy = $derived(recomputing || runActive);
  const activeRunKey = $derived(
    !permissionRevoked &&
      data.permissions?.read === true &&
      runActive &&
      data.selectedOpportunity?.id &&
      currentRun?.id
      ? `${data.selectedOpportunity.id}:${currentRun.id}`
      : ''
  );
  const activeFilterCount = $derived(
    [data.filters.q, data.filters.status, data.filters.type, data.filters.matchStatus].filter(
      Boolean
    ).length
  );

  $effect(() => {
    const matches = data.matches;
    if (matches.length === 0) {
      selectedMatchId = null;
    } else if (!selectedMatchId || !matches.some((match) => match.id === selectedMatchId)) {
      selectedMatchId = matches[0].id;
    }
  });

  /** @param {Record<string, string | null>} changes */
  function updateQuery(changes) {
    const url = new URL($page.url);
    for (const [key, value] of Object.entries(changes)) {
      if (!value || value === 'ALL') url.searchParams.delete(key);
      else url.searchParams.set(key, value);
    }
    // The destination is the current URL object, so it already includes any configured base path.
    // eslint-disable-next-line svelte/no-navigation-without-resolve
    return goto(url, { keepFocus: true, noScroll: true, replaceState: true });
  }

  /** @param {string} id */
  function selectOpportunity(id) {
    selectedMatchId = null;
    evidenceSheetOpen = false;
    polledRun = null;
    polledRunHistory = [];
    updateQuery({ opportunity: id, run: null });
  }

  function clearFilters() {
    updateQuery({
      q: null,
      status: null,
      type: null,
      match_status: null,
      opportunity: null,
      run: null
    });
  }

  /** @param {any} match */
  function selectMatch(match) {
    selectedMatchId = match.id;
  }

  /** @param {any} match */
  function openEvidence(match) {
    selectedMatchId = match.id;
    evidenceSheetOpen = true;
  }

  /** @param {string} status */
  function openDecision(status) {
    if (!canDecide || !selectedMatch || decisionBusyId) return;
    if (!decisionTargetsForStatus(selectedMatch.status).includes(status)) return;
    pendingDecision = status;
    pendingReasonCode = decisionReasonCodes[status] || '';
    decisionReason = '';
    decisionIdempotencyKey = globalThis.crypto?.randomUUID?.() || '';
    decisionDialogOpen = true;
  }

  function openRecompute() {
    if (!canRecompute || recomputeBusy) return;
    recomputeIdempotencyKey = globalThis.crypto?.randomUUID?.() || '';
    recomputeDialogOpen = true;
  }

  function recomputeEnhance() {
    recomputing = true;
    liveMessage = 'Queueing candidate ranking. Please wait.';
    return async ({ result, update }) => {
      recomputing = false;
      const actionData = /** @type {any} */ (result).data;
      if (result.type === 'success' && actionData?.run?.id) {
        recomputeDialogOpen = false;
        recomputeIdempotencyKey = '';
        polledRun = actionData.run;
        polledRunHistory = [
          actionData.run,
          ...runHistory.filter((run) => run.id !== actionData.run.id)
        ].slice(0, 10);
        handledTerminalRunId = '';
        liveMessage = 'Candidate ranking was queued and will update in the background.';
        toast.success('Candidate ranking queued');
        await update({ reset: false, invalidateAll: false });
        await updateQuery({ run: actionData.run.id });
      } else {
        liveMessage = actionData?.actionError || 'Candidates could not be recomputed.';
        toast.error(liveMessage);
      }
    };
  }

  function statusEnhance() {
    if (!selectedMatch) return;
    decisionBusyId = selectedMatch.id;
    liveMessage = `Saving ${pendingDecision} decision for ${selectedMatch.personName}.`;
    return async ({ result, update }) => {
      const actionData = /** @type {any} */ (result).data;
      const personName = selectedMatch?.personName || 'candidate';
      decisionBusyId = null;
      if (result.type === 'success') {
        decisionDialogOpen = false;
        liveMessage = `${personName} is now ${pendingDecision}.`;
        toast.success(`Decision saved: ${pendingDecision}`);
        await update({ reset: false });
        pendingDecision = '';
        pendingReasonCode = '';
        decisionReason = '';
        decisionIdempotencyKey = '';
      } else if (actionData?.conflict) {
        liveMessage = actionData.actionError;
        toast.error(liveMessage);
        await update({ reset: false, invalidateAll: false });
        await invalidateAll();
        decisionDialogOpen = true;
      } else {
        liveMessage = actionData?.actionError || 'The decision could not be saved.';
        toast.error(liveMessage);
        await update({ reset: false, invalidateAll: false });
      }
    };
  }

  function stopPolling() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  async function pollRunStatus() {
    if (pollInFlight || !currentRun?.id || !data.selectedOpportunity?.id) return;
    pollInFlight = true;
    try {
      const params = new URLSearchParams({
        run: currentRun.id,
        opportunity: data.selectedOpportunity.id,
        _: String(Date.now())
      });
      const response = await fetch(`/api/matching-run-poll?${params}`);
      if (response.status === 403) {
        stopPolling();
        if (!permissionRevoked) {
          permissionRevoked = true;
          recomputeDialogOpen = false;
          decisionDialogOpen = false;
          liveMessage = 'Your matching access changed. Ranking updates have stopped.';
          toast.error('Matching access changed');
        }
        return;
      }
      if (!response.ok) throw new Error('poll unavailable');
      const result = await response.json();
      if (!result.run?.id) throw new Error('run unavailable');
      polledRun = result.run;
      if (Array.isArray(result.runs)) polledRunHistory = result.runs;
      pollFailures = 0;

      if (isMatchRunTerminal(result.run)) {
        stopPolling();
        if (handledTerminalRunId !== result.run.id) {
          handledTerminalRunId = result.run.id;
          if (isMatchRunSkipped(result.run)) {
            liveMessage =
              'Candidate ranking was skipped because the opportunity is no longer active.';
            toast.info('Ranking skipped');
            await invalidateAll();
          } else if (isMatchRunSuccessful(result.run)) {
            const count = Number(result.run.resultCount) || 0;
            liveMessage = `Candidate ranking completed. ${count} candidates ranked.`;
            toast.success(`Ranking completed: ${count} candidates`);
            await invalidateAll();
          } else {
            const errorCode = result.run.errorCode || 'MATCH_RECOMPUTE_FAILED';
            liveMessage = `Candidate ranking stopped. Error code: ${errorCode}.`;
            toast.error(`Ranking failed: ${errorCode}`);
          }
        }
      }
    } catch {
      pollFailures += 1;
      if (pollFailures === 3) {
        liveMessage = 'Ranking status is temporarily unavailable. Retrying.';
      }
    } finally {
      pollInFlight = false;
    }
  }

  function startPolling() {
    stopPolling();
    pollRunStatus();
    pollTimer = setInterval(pollRunStatus, 3000);
  }

  $effect(() => {
    const key = activeRunKey;
    if (key) startPolling();
    else stopPolling();
    return () => stopPolling();
  });

  /** @param {string} value */
  function label(value) {
    return value
      ? value
          .split('_')
          .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
          .join(' ')
      : 'Unknown';
  }

  /** @param {string} value */
  function formatDate(value) {
    if (!value) return 'Not recorded';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return 'Not recorded';
    return new Intl.DateTimeFormat('en', {
      year: 'numeric',
      month: 'short',
      day: 'numeric'
    }).format(date);
  }

  /** @param {string} value */
  function statusClass(value) {
    if (value === 'accepted' || value === 'open') {
      return 'border-transparent bg-[color:var(--green-soft)] text-[color:var(--green-soft-text)]';
    }
    if (value === 'rejected' || value === 'closed') {
      return 'border-transparent bg-[color:var(--red-soft)] text-[color:var(--red)]';
    }
    if (value === 'shortlisted' || value === 'paused') {
      return 'border-transparent bg-[color:var(--amber-soft)] text-[color:var(--amber-soft-text)]';
    }
    return 'border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] text-[color:var(--text-muted)]';
  }

  /** @param {Record<string, string[]>} criteria */
  function criteriaEntries(criteria) {
    return Object.entries(criteria || {}).filter(
      ([, values]) => Array.isArray(values) && values.length
    );
  }
</script>

<svelte:head>
  <title>Matching workbench - BottleCRM</title>
</svelte:head>

<div class="flex min-h-0 flex-col">
  <PageHeader
    title="Matching workbench"
    subtitle="Place the right person into the right opportunity with evidence-backed ranking"
  >
    {#snippet actions()}
      {#if canRecompute}
        <Button
          size="sm"
          class="gap-1.5"
          disabled={!data.selectedOpportunity || recomputeBusy}
          aria-busy={recomputeBusy}
          onclick={openRecompute}
        >
          <RefreshCw
            class="size-3.5 {recomputeBusy ? 'animate-spin motion-reduce:animate-none' : ''}"
          />
          {runActive ? 'Ranking in progress…' : recomputing ? 'Queueing…' : 'Recompute candidates'}
        </Button>
      {:else if permissionRevoked}
        <Badge variant="outline">Access changed</Badge>
      {:else if canManage}
        <Badge variant="outline">Manage access</Badge>
      {:else if isReadOnly}
        <Badge variant="outline">Read only</Badge>
      {/if}
    {/snippet}
  </PageHeader>

  <FilterStrip>
    <SearchInput
      value={data.filters.q}
      placeholder="Search opportunities…"
      onchange={(value) => updateQuery({ q: value, opportunity: null, run: null })}
      class="w-full sm:w-56"
    />
    <SelectFilter
      options={opportunityStatusOptions}
      value={data.filters.status || 'ALL'}
      allLabel="All opportunity statuses"
      onchange={(value) => updateQuery({ status: String(value), opportunity: null, run: null })}
    />
    <SelectFilter
      options={opportunityTypeOptions}
      value={data.filters.type || 'ALL'}
      allLabel="All opportunity types"
      onchange={(value) => updateQuery({ type: String(value), opportunity: null, run: null })}
    />
    <SelectFilter
      options={matchStatusOptions}
      value={data.filters.matchStatus || 'ALL'}
      allLabel="All candidate statuses"
      onchange={(value) => updateQuery({ match_status: String(value) })}
    />
    {#if activeFilterCount > 0}
      <FilterPill label="Clear all" dashed onclick={clearFilters} />
    {/if}
    {#snippet meta()}
      <span>{data.opportunities.length} opportunities</span>
    {/snippet}
  </FilterStrip>

  <p class="sr-only" role="status" aria-live="polite">{liveMessage}</p>

  {#if data.opportunities.length === 0}
    <section class="flex flex-1 flex-col items-center justify-center px-6 py-20 text-center">
      <div
        class="mb-4 flex size-14 items-center justify-center rounded-full bg-[color:var(--violet-soft)]"
      >
        <Target class="size-7 text-[color:var(--violet-soft-text)]" />
      </div>
      <h2 class="text-lg font-semibold text-[color:var(--text)]">No matching opportunities</h2>
      <p class="mt-1 max-w-md text-sm text-[color:var(--text-muted)]">
        {activeFilterCount > 0
          ? 'No opportunities meet the current filters.'
          : 'Create or import a matching opportunity before ranking people.'}
      </p>
      {#if activeFilterCount > 0}
        <Button variant="outline" class="mt-4" onclick={clearFilters}>Clear filters</Button>
      {/if}
    </section>
  {:else}
    <div
      class="grid min-h-[calc(100vh-170px)] grid-cols-1 border-t border-[color:var(--border-faint)] lg:grid-cols-[280px_minmax(0,1fr)] xl:grid-cols-[280px_minmax(0,1fr)_380px]"
    >
      <aside
        class="max-h-[320px] overflow-y-auto border-b border-[color:var(--border-faint)] bg-[color:var(--bg-card)] lg:max-h-none lg:border-r lg:border-b-0"
        aria-labelledby="matching-opportunities-title"
      >
        <div
          class="sticky top-0 z-[1] border-b border-[color:var(--border-faint)] bg-[color:var(--bg-card)] px-4 py-3"
        >
          <h2
            id="matching-opportunities-title"
            class="text-xs font-semibold tracking-wide text-[color:var(--text-subtle)] uppercase"
          >
            Opportunities
          </h2>
        </div>
        <ol class="divide-y divide-[color:var(--border-faint)]">
          {#each data.opportunities as opportunity (opportunity.id)}
            <li>
              <button
                type="button"
                aria-current={data.selectedOpportunity?.id === opportunity.id ? 'true' : undefined}
                class="group w-full px-4 py-3 text-left transition-colors focus-visible:shadow-[inset_3px_0_0_var(--violet),0_0_0_2px_var(--focus-ring)] focus-visible:outline-none {data
                  .selectedOpportunity?.id === opportunity.id
                  ? 'bg-[color:var(--violet-soft)]'
                  : 'hover:bg-[color:var(--bg-hover)]'}"
                onclick={() => selectOpportunity(opportunity.id)}
              >
                <span class="flex items-start justify-between gap-3">
                  <span class="min-w-0">
                    <span class="block truncate text-sm font-semibold text-[color:var(--text)]"
                      >{opportunity.title}</span
                    >
                    <span
                      class="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-[color:var(--text-subtle)]"
                    >
                      <span>{label(opportunity.type)}</span>
                      {#if opportunity.location}<span>· {opportunity.location}</span>{/if}
                    </span>
                  </span>
                  <ChevronRight
                    class="mt-0.5 size-4 shrink-0 text-[color:var(--text-subtle)]"
                    aria-hidden="true"
                  />
                </span>
                <span class="mt-2 flex items-center justify-between gap-2">
                  <Badge variant="outline" class={statusClass(opportunity.status)}
                    >{label(opportunity.status)}</Badge
                  >
                  <span class="text-xs text-[color:var(--text-subtle)] tabular-nums"
                    >{opportunity.matchCount} matches</span
                  >
                </span>
              </button>
            </li>
          {/each}
        </ol>
      </aside>

      <main class="min-w-0 bg-[color:var(--bg)]" aria-labelledby="candidate-ranking-title">
        {#if data.selectedOpportunity}
          <section class="border-b border-[color:var(--border-faint)] px-5 py-4 md:px-6">
            <div class="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
              <div class="min-w-0">
                <div class="flex flex-wrap items-center gap-2">
                  <Badge variant="outline" class={statusClass(data.selectedOpportunity.status)}>
                    {label(data.selectedOpportunity.status)}
                  </Badge>
                  <span class="text-xs text-[color:var(--text-subtle)]"
                    >{label(data.selectedOpportunity.type)}</span
                  >
                </div>
                <h2
                  id="candidate-ranking-title"
                  class="mt-2 text-xl font-semibold text-[color:var(--text)]"
                >
                  {data.selectedOpportunity.title}
                </h2>
                {#if data.selectedOpportunity.description}
                  <p class="mt-1 line-clamp-2 text-sm text-[color:var(--text-muted)]">
                    {data.selectedOpportunity.description}
                  </p>
                {/if}
              </div>
              <div class="flex shrink-0 flex-wrap gap-3 text-xs text-[color:var(--text-subtle)]">
                {#if data.selectedOpportunity.organizationName}
                  <span class="inline-flex items-center gap-1"
                    ><Building2 class="size-3.5" />{data.selectedOpportunity.organizationName}</span
                  >
                {/if}
                {#if data.selectedOpportunity.location}
                  <span class="inline-flex items-center gap-1"
                    ><MapPin class="size-3.5" />{data.selectedOpportunity.location}</span
                  >
                {/if}
              </div>
            </div>

            {#if criteriaEntries(data.selectedOpportunity.requiredCriteria).length > 0}
              <div class="mt-4 flex flex-wrap items-center gap-2" aria-label="Required criteria">
                <span
                  class="text-[11px] font-semibold tracking-wide text-[color:var(--text-subtle)] uppercase"
                  >Required</span
                >
                {#each criteriaEntries(data.selectedOpportunity.requiredCriteria) as [dimension, values] (dimension)}
                  {#each values as value (`${dimension}:${value}`)}
                    <Badge variant="neutral">{label(dimension)}: {value}</Badge>
                  {/each}
                {/each}
              </div>
            {/if}
          </section>

          {#if currentRun}
            <section
              class="border-b border-[color:var(--border-faint)] bg-[color:var(--bg-card)] px-5 py-3 md:px-6"
              aria-labelledby="ranking-run-title"
            >
              <div class="flex flex-wrap items-center justify-between gap-2">
                <div class="min-w-0">
                  <h3 id="ranking-run-title" class="text-xs font-semibold text-[color:var(--text)]">
                    Ranking run
                    {#if currentRun.rankingRevision > 0}
                      · version {currentRun.rankingRevision}
                    {/if}
                  </h3>
                  <p class="mt-0.5 text-[11px] text-[color:var(--text-subtle)]">
                    {label(currentRun.status || currentRun.outcome)} · created {formatDate(
                      currentRun.createdAt
                    )}
                    {#if currentRun.engineVersion}
                      · engine {currentRun.engineVersion}{/if}
                  </p>
                </div>
                <Badge variant="outline">{label(currentRun.status || currentRun.outcome)}</Badge>
              </div>

              {#if runActive}
                <div class="mt-3">
                  <div
                    class="mb-1 flex items-center justify-between text-xs text-[color:var(--text-muted)]"
                  >
                    <span
                      >{currentRun.processedCount} of {currentRun.totalCount || '…'} processed</span
                    >
                    <span class="tabular-nums">{currentRun.progress}%</span>
                  </div>
                  <Progress
                    value={currentRun.progress}
                    max={100}
                    aria-label="Ranking progress: {currentRun.progress}%"
                    class="h-1.5"
                  />
                  {#if pollFailures >= 3}
                    <p class="mt-2 text-xs text-[color:var(--amber-soft-text)]">
                      Status connection interrupted; retrying automatically.
                    </p>
                  {/if}
                </div>
              {:else if isMatchRunSkipped(currentRun)}
                <p class="mt-3 text-xs text-[color:var(--text-muted)]">
                  Skipped because the opportunity is paused, filled, or closed.
                </p>
              {:else if isMatchRunSuccessful(currentRun)}
                <p class="mt-2 text-xs text-[color:var(--green-soft-text)]">
                  Completed with {currentRun.resultCount} ranked candidates.
                </p>
              {:else if isMatchRunTerminal(currentRun)}
                <p class="mt-2 flex items-center gap-1.5 text-xs text-[color:var(--red)]">
                  <CircleAlert class="size-3.5" />Ranking stopped. Error code:
                  <code>{currentRun.errorCode || 'MATCH_RECOMPUTE_FAILED'}</code>
                </p>
              {/if}

              {#if runHistory.length > 0}
                <details class="mt-3 text-xs text-[color:var(--text-muted)]">
                  <summary
                    class="cursor-pointer rounded-sm font-medium focus-visible:ring-2 focus-visible:ring-[color:var(--focus-ring)] focus-visible:outline-none"
                  >
                    Recent ranking versions ({runHistory.length})
                  </summary>
                  <ol class="mt-2 grid gap-1 sm:grid-cols-2">
                    {#each runHistory as run (run.id)}
                      <li
                        class="flex items-center justify-between gap-3 rounded-md border border-[color:var(--border-faint)] px-2.5 py-2"
                      >
                        <span class="min-w-0 truncate">
                          {run.rankingRevision > 0
                            ? `Version ${run.rankingRevision}`
                            : 'Pending version'}
                          · {label(run.status || run.outcome)}
                        </span>
                        <span class="shrink-0 tabular-nums">{run.resultCount} results</span>
                      </li>
                    {/each}
                  </ol>
                </details>
              {/if}
            </section>
          {/if}

          {#if data.matches.length === 0}
            <section class="flex flex-col items-center justify-center px-6 py-20 text-center">
              <Users class="size-9 text-[color:var(--text-subtle)]" />
              <h3 class="mt-3 text-base font-semibold text-[color:var(--text)]">
                No candidates to review
              </h3>
              <p class="mt-1 max-w-md text-sm text-[color:var(--text-muted)]">
                {data.filters.matchStatus
                  ? 'No candidates have this decision status.'
                  : runActive
                    ? 'Candidate ranking is running in the background.'
                    : 'Run an explicit recompute when people and evidence are ready.'}
              </p>
            </section>
          {:else}
            <div
              class="flex items-center justify-between border-b border-[color:var(--border-faint)] px-5 py-2.5 text-xs text-[color:var(--text-subtle)] md:px-6"
            >
              <span>{data.totalMatches} ranked candidates</span>
              <span>{data.counts.shortlisted} shortlisted · {data.counts.accepted} accepted</span>
            </div>
            <ol aria-label="Candidate ranking" class="divide-y divide-[color:var(--border-faint)]">
              {#each data.matches as match (match.id)}
                <li
                  class="flex items-stretch {selectedMatch?.id === match.id
                    ? 'bg-[color:var(--bg-active)]'
                    : ''}"
                >
                  <button
                    type="button"
                    aria-pressed={selectedMatch?.id === match.id}
                    class="group flex min-w-0 flex-1 items-start gap-3 px-5 py-4 text-left transition-colors hover:bg-[color:var(--bg-hover)] focus-visible:shadow-[inset_3px_0_0_var(--violet),0_0_0_2px_var(--focus-ring)] focus-visible:outline-none md:px-6"
                    onclick={() => selectMatch(match)}
                  >
                    <span
                      class="flex size-8 shrink-0 items-center justify-center rounded-full bg-[color:var(--bg-elevated)] text-sm font-bold text-[color:var(--text-muted)] tabular-nums"
                    >
                      {match.rank || '–'}
                    </span>
                    <span class="min-w-0 flex-1">
                      <span class="flex flex-wrap items-center gap-2">
                        <span class="truncate text-sm font-semibold text-[color:var(--text)]"
                          >{match.personName}</span
                        >
                        <Badge variant="outline" class={statusClass(match.status)}
                          >{label(match.status)}</Badge
                        >
                      </span>
                      {#if match.personSummary.currentTitle || match.personSummary.currentCompany}
                        <span class="mt-1 block text-xs text-[color:var(--text-subtle)]">
                          {[match.personSummary.currentTitle, match.personSummary.currentCompany]
                            .filter(Boolean)
                            .join(' · ')}
                        </span>
                      {/if}
                      <span class="mt-1 block text-xs text-[color:var(--text-muted)]">
                        {match.reasons[0]?.message ||
                          match.gaps[0]?.message ||
                          'No explanatory criteria recorded.'}
                      </span>
                      <span
                        class="mt-2 flex flex-wrap items-center gap-3 text-[11px] text-[color:var(--text-subtle)]"
                      >
                        <span>{Math.round(match.confidence * 100)}% confidence</span>
                        <span>{match.evidenceLinks.length} evidence items</span>
                        {#if match.gaps.length > 0}<span>{match.gaps.length} gaps</span>{/if}
                      </span>
                    </span>
                    <span class="shrink-0 text-right">
                      <span class="block text-2xl font-bold text-[color:var(--text)] tabular-nums"
                        >{match.overallScore}</span
                      >
                      <span
                        class="block text-[10px] tracking-wide text-[color:var(--text-subtle)] uppercase"
                        >{scoreLabel(match.overallScore)}</span
                      >
                    </span>
                  </button>
                  <button
                    type="button"
                    class="flex w-11 shrink-0 items-center justify-center border-l border-[color:var(--border-faint)] text-[color:var(--text-subtle)] hover:bg-[color:var(--bg-hover)] hover:text-[color:var(--text)] focus-visible:shadow-[inset_0_0_0_2px_var(--focus-ring)] focus-visible:outline-none xl:hidden"
                    aria-label="View evidence for {match.personName}"
                    onclick={() => openEvidence(match)}
                  >
                    <Eye class="size-4" />
                  </button>
                </li>
              {/each}
            </ol>
          {/if}
        {/if}
      </main>

      <aside
        class="hidden min-w-0 border-l border-[color:var(--border-faint)] bg-[color:var(--bg-card)] xl:block"
        aria-label="Match evidence"
      >
        {#if selectedMatch}
          {@render evidenceContent(selectedMatch)}
        {:else}
          <div
            class="flex h-full flex-col items-center justify-center px-6 text-center text-sm text-[color:var(--text-subtle)]"
          >
            Select a candidate to inspect the evidence.
          </div>
        {/if}
      </aside>
    </div>
  {/if}
</div>

{#snippet evidenceContent(match)}
  <div class="flex h-full min-h-0 flex-col">
    <div class="border-b border-[color:var(--border-faint)] px-5 py-4">
      <div class="flex items-start justify-between gap-4">
        <div class="min-w-0">
          <p
            class="text-[11px] font-semibold tracking-wide text-[color:var(--text-subtle)] uppercase"
          >
            Evidence review
          </p>
          <h2 class="mt-1 truncate text-lg font-semibold text-[color:var(--text)]">
            {match.personName}
          </h2>
        </div>
        <div class="text-right">
          <span class="block text-3xl font-bold text-[color:var(--text)] tabular-nums"
            >{match.overallScore}</span
          >
          <span class="text-[10px] tracking-wide text-[color:var(--text-subtle)] uppercase"
            >{scoreLabel(match.overallScore)}</span
          >
        </div>
      </div>
      {#if canDecide && decisionTargetsForStatus(match.status).length > 0}
        <div class="mt-4 grid grid-cols-2 gap-2">
          {#if decisionTargetsForStatus(match.status).includes('reviewing')}
            <Button
              size="sm"
              variant="outline"
              disabled={decisionBusyId === match.id}
              onclick={() => openDecision('reviewing')}
            >
              <Activity />Review
            </Button>
          {/if}
          {#if decisionTargetsForStatus(match.status).includes('shortlisted')}
            <Button
              size="sm"
              variant="outline"
              disabled={decisionBusyId === match.id}
              onclick={() => openDecision('shortlisted')}
            >
              <Sparkles />Shortlist
            </Button>
          {/if}
          {#if decisionTargetsForStatus(match.status).includes('accepted')}
            <Button
              size="sm"
              variant="outline"
              disabled={decisionBusyId === match.id}
              onclick={() => openDecision('accepted')}
            >
              <Check />Accept
            </Button>
          {/if}
          {#if decisionTargetsForStatus(match.status).includes('rejected')}
            <Button
              size="sm"
              variant="destructive"
              disabled={decisionBusyId === match.id}
              onclick={() => openDecision('rejected')}
            >
              <X />Reject
            </Button>
          {/if}
        </div>
      {:else if decisionTargetsForStatus(match.status).length === 0}
        <p class="mt-4 text-xs text-[color:var(--text-subtle)]">
          This decision is final. No further manual transition is available.
        </p>
      {:else}
        <p class="mt-4 text-xs text-[color:var(--text-subtle)]">
          You do not have permission to change this decision.
        </p>
      {/if}
      {#if match.decisionReason}
        <p
          class="mt-3 rounded-md bg-[color:var(--bg-elevated)] px-3 py-2 text-xs text-[color:var(--text-muted)]"
        >
          Decision note: {match.decisionReason}
        </p>
      {/if}
    </div>

    <div class="flex-1 space-y-6 overflow-y-auto px-5 py-5">
      <section aria-labelledby="score-breakdown-title">
        <h3
          id="score-breakdown-title"
          class="text-xs font-semibold tracking-wide text-[color:var(--text-subtle)] uppercase"
        >
          Score breakdown
        </h3>
        <div class="mt-3 space-y-3">
          {#each [['Eligibility', match.eligibilityScore], ['Fit', match.fitScore], ['Trust', match.trustScore], ['Relationship', match.relationshipScore], ['Availability', match.availabilityScore]] as [scoreName, score] (scoreName)}
            <div>
              <div class="mb-1 flex items-center justify-between text-xs">
                <span class="text-[color:var(--text-muted)]">{scoreName}</span>
                <span class="font-semibold text-[color:var(--text)] tabular-nums">{score}/100</span>
              </div>
              <Progress
                value={score}
                max={100}
                aria-label="{scoreName} score: {score} out of 100"
                class="h-1.5"
              />
            </div>
          {/each}
        </div>
      </section>

      <section aria-labelledby="match-reasons-title">
        <h3
          id="match-reasons-title"
          class="flex items-center gap-2 text-xs font-semibold tracking-wide text-[color:var(--text-subtle)] uppercase"
        >
          <ShieldCheck class="size-3.5" />Why this person
        </h3>
        {#if match.reasons.length > 0}
          <ul class="mt-3 space-y-2">
            {#each match.reasons as reason (`${reason.dimension}:${reason.message}`)}
              <li
                class="rounded-md bg-[color:var(--green-soft)] px-3 py-2 text-xs text-[color:var(--green-soft-text)]"
              >
                {reason.message}
              </li>
            {/each}
          </ul>
        {:else}
          <p class="mt-2 text-xs text-[color:var(--text-subtle)]">
            No positive criteria were recorded.
          </p>
        {/if}
      </section>

      {#if match.gaps.length > 0}
        <section aria-labelledby="match-gaps-title">
          <h3
            id="match-gaps-title"
            class="flex items-center gap-2 text-xs font-semibold tracking-wide text-[color:var(--text-subtle)] uppercase"
          >
            <CircleAlert class="size-3.5" />Gaps and exclusions
          </h3>
          <ul class="mt-3 space-y-2">
            {#each match.gaps as gap (`${gap.dimension}:${gap.message}`)}
              <li
                class="rounded-md bg-[color:var(--amber-soft)] px-3 py-2 text-xs text-[color:var(--amber-soft-text)]"
              >
                {gap.message}
              </li>
            {/each}
          </ul>
        </section>
      {/if}

      <section aria-labelledby="supporting-evidence-title">
        <div class="flex items-center justify-between gap-2">
          <h3
            id="supporting-evidence-title"
            class="text-xs font-semibold tracking-wide text-[color:var(--text-subtle)] uppercase"
          >
            Supporting evidence
          </h3>
          <span class="text-xs text-[color:var(--text-subtle)] tabular-nums"
            >{match.evidenceLinks.length}</span
          >
        </div>
        {#if match.evidenceLinks.length > 0}
          <ol class="mt-3 space-y-3">
            {#each match.evidenceLinks as link, index (link.id || index)}
              <li
                class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-3"
              >
                <div class="flex flex-wrap items-center gap-2">
                  <Badge variant="neutral">{label(link.evidence.source)}</Badge>
                  <span class="text-[11px] text-[color:var(--text-subtle)]"
                    >{label(link.evidence.kind)}</span
                  >
                  <span class="ml-auto text-[11px] text-[color:var(--text-subtle)] tabular-nums"
                    >{Math.round(link.evidence.confidence * 100)}% confidence</span
                  >
                </div>
                <p class="mt-2 text-xs leading-5 text-[color:var(--text-muted)]">
                  {link.evidence.summary || 'No evidence summary available.'}
                </p>
                {#if link.explanation}
                  <p class="mt-2 text-[11px] text-[color:var(--violet-soft-text)]">
                    Contribution: {link.explanation}
                  </p>
                {/if}
                <div
                  class="mt-2 flex items-center gap-1 text-[11px] text-[color:var(--text-subtle)]"
                >
                  <Clock class="size-3" />
                  <time datetime={link.evidence.observedAt}
                    >{formatDate(link.evidence.observedAt)}</time
                  >
                </div>
              </li>
            {/each}
          </ol>
        {:else}
          <p class="mt-2 text-xs text-[color:var(--text-subtle)]">
            No supporting evidence was attached to this match.
          </p>
        {/if}
      </section>

      <p
        class="border-t border-[color:var(--border-faint)] pt-4 text-[11px] text-[color:var(--text-subtle)]"
      >
        Evaluated <time datetime={match.evaluatedAt}>{formatDate(match.evaluatedAt)}</time>
        {#if match.engineVersion}
          · Engine {match.engineVersion}{/if}
        {#if match.rankingRevision > 0}
          · Ranking version {match.rankingRevision}{/if}
        · Decision revision {match.decisionRevision}
      </p>
    </div>
  </div>
{/snippet}

<Sheet.Root bind:open={evidenceSheetOpen}>
  <Sheet.Content
    side="right"
    class="w-full max-w-full gap-0 overflow-hidden p-0 sm:w-[480px] sm:max-w-[480px] xl:hidden"
  >
    <Sheet.Header class="sr-only">
      <Sheet.Title>Match evidence for {selectedMatch?.personName || 'candidate'}</Sheet.Title>
      <Sheet.Description>Scores, reasons, gaps and source-attributed evidence.</Sheet.Description>
    </Sheet.Header>
    {#if selectedMatch}
      {@render evidenceContent(selectedMatch)}
    {/if}
  </Sheet.Content>
</Sheet.Root>

<AlertDialog.Root bind:open={recomputeDialogOpen}>
  <AlertDialog.Content class="max-w-md">
    <AlertDialog.Header>
      <AlertDialog.Title>Recompute candidate ranking?</AlertDialog.Title>
      <AlertDialog.Description>
        This queues a background ranking run for “{data.selectedOpportunity?.title ||
          'this opportunity'}”. Progress and safe failure codes will appear here. Human review
        decisions are preserved.
      </AlertDialog.Description>
    </AlertDialog.Header>
    <AlertDialog.Footer>
      <AlertDialog.Cancel disabled={recomputeBusy}>Cancel</AlertDialog.Cancel>
      <AlertDialog.Action disabled={recomputeBusy} onclick={() => recomputeForm.requestSubmit()}>
        Queue recompute
      </AlertDialog.Action>
    </AlertDialog.Footer>
  </AlertDialog.Content>
</AlertDialog.Root>

<form
  method="POST"
  action="?/recompute"
  bind:this={recomputeForm}
  use:enhance={recomputeEnhance}
  class="hidden"
>
  <input type="hidden" name="opportunity_id" value={data.selectedOpportunity?.id || ''} />
  <input type="hidden" name="idempotency_key" value={recomputeIdempotencyKey} />
</form>

<Dialog.Root bind:open={decisionDialogOpen}>
  <Dialog.Content class="sm:max-w-md">
    <Dialog.Header>
      <Dialog.Title
        >{label(pendingDecision)} {selectedMatch?.personName || 'candidate'}?</Dialog.Title
      >
      <Dialog.Description>
        This records an auditable decision against decision revision {selectedMatch?.decisionRevision ||
          0} and ranking version {selectedMatch?.rankingRevision || 0}. If either changed, you will
        be asked to review the latest version.
      </Dialog.Description>
    </Dialog.Header>

    <form method="POST" action="?/setStatus" use:enhance={statusEnhance} class="space-y-4">
      <input type="hidden" name="match_id" value={selectedMatch?.id || ''} />
      <input type="hidden" name="status" value={pendingDecision} />
      <input type="hidden" name="reason_code" value={pendingReasonCode} />
      <input type="hidden" name="expected_revision" value={selectedMatch?.decisionRevision || 0} />
      <input
        type="hidden"
        name="expected_ranking_revision"
        value={selectedMatch?.rankingRevision || 0}
      />
      <input type="hidden" name="idempotency_key" value={decisionIdempotencyKey} />

      <div
        class="rounded-md bg-[color:var(--bg-elevated)] px-3 py-2 text-xs text-[color:var(--text-muted)]"
      >
        Reason category: <span class="font-semibold text-[color:var(--text)]"
          >{label(pendingReasonCode)}</span
        >
      </div>

      <div class="space-y-1.5">
        <label for="matching-decision-reason" class="text-xs font-medium text-[color:var(--text)]">
          Decision note <span class="font-normal text-[color:var(--text-subtle)]">(optional)</span>
        </label>
        <Textarea
          id="matching-decision-reason"
          name="reason"
          bind:value={decisionReason}
          maxlength={1000}
          placeholder="Add concise context for the next reviewer…"
        />
        <p class="text-right text-[11px] text-[color:var(--text-subtle)] tabular-nums">
          {decisionReason.length}/1000
        </p>
      </div>

      <Dialog.Footer>
        <Button
          type="button"
          variant="outline"
          disabled={decisionBusyId === selectedMatch?.id}
          onclick={() => (decisionDialogOpen = false)}
        >
          Cancel
        </Button>
        <Button type="submit" disabled={!pendingReasonCode || decisionBusyId === selectedMatch?.id}>
          {decisionBusyId === selectedMatch?.id ? 'Saving…' : 'Confirm decision'}
        </Button>
      </Dialog.Footer>
    </form>
  </Dialog.Content>
</Dialog.Root>
