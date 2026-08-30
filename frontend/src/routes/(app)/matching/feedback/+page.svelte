<script>
  import { enhance } from '$app/forms';
  import { goto } from '$app/navigation';
  import { page } from '$app/stores';
  import { toast } from 'svelte-sonner';
  import {
    AlertTriangle,
    ArrowLeft,
    BarChart3,
    CheckCircle2,
    Eye,
    Gauge,
    History,
    Loader2,
    RefreshCw,
    ShieldCheck,
    Sparkles,
    Target
  } from '@lucide/svelte';

  import MatchFeedbackDialog from '$lib/components/matching/MatchFeedbackDialog.svelte';
  import WeightSuggestionReviewDialog from '$lib/components/matching/WeightSuggestionReviewDialog.svelte';
  import { FilterPill, FilterStrip, PageHeader } from '$lib/components/layout';
  import * as AlertDialog from '$lib/components/ui/alert-dialog/index.js';
  import { Badge } from '$lib/components/ui/badge/index.js';
  import { Button } from '$lib/components/ui/button/index.js';
  import { SelectFilter } from '$lib/components/ui/filter/index.js';
  import { Progress } from '$lib/components/ui/progress/index.js';
  import {
    EVIDENCE_DIMENSIONS,
    FEEDBACK_QUEUES,
    SCORING_DIMENSIONS
  } from '$lib/matching/feedback.js';
  import { OPPORTUNITY_TYPES } from '$lib/matching/workbench.js';

  let { data, form } = $props();

  const typeOptions = OPPORTUNITY_TYPES.map((value) => ({ value, label: label(value) }));
  const queueOptions = FEEDBACK_QUEUES.map((value) => ({ value, label: label(value) }));

  let actionError = $state('');
  let busyAction = $state('');
  let permissionRevoked = $state(false);
  let feedbackDialogOpen = $state(false);
  let presentedMatchId = $state('');
  let feedbackKey = $state('');
  let outcomeKey = $state('');
  let suggestionDialogOpen = $state(false);
  let presentedSuggestionId = $state('');
  let suggestionKey = $state('');
  let generateKey = $state('');
  let presentedGenerateType = $state('');
  let publishDialogOpen = $state(false);
  let publishVersion = $state(/** @type {any} */ (null));
  let publishKey = $state('');
  let liveMessage = $state('');

  const canFeedback = $derived(data.permissions?.feedback === true && !permissionRevoked);
  const canCalibrate = $derived(data.permissions?.calibrate === true && !permissionRevoked);
  const activeFilterCount = $derived(
    [data.filters.type, data.filters.queue].filter(Boolean).length
  );
  const summaryCards = $derived([
    {
      label: 'Feedback captured',
      value: data.overview.summary.feedbackCaptured,
      detail: `${data.overview.summary.feedbackDue} due`,
      icon: CheckCircle2
    },
    {
      label: 'Coverage',
      value: `${Math.round(data.overview.summary.feedbackCoverage * 100)}%`,
      detail: 'Structured recommendation feedback',
      icon: Gauge
    },
    {
      label: 'Recorded outcomes',
      value: data.overview.summary.outcomeKnown,
      detail: 'Observed lifecycle milestones',
      icon: Target
    },
    {
      label: 'Useful assessment rate',
      value: `${Math.round(data.overview.summary.accuracyAgreement * 100)}%`,
      detail: `${data.overview.summary.accuracySampleSize} observations`,
      icon: BarChart3
    },
    {
      label: 'AI suggestions',
      value: data.overview.summary.pendingSuggestions,
      detail: 'Pending human review',
      icon: Sparkles
    }
  ]);

  $effect(() => {
    if (form?.actionError) actionError = String(form.actionError);
  });

  $effect(() => {
    const matchId = data.selected?.match?.id || '';
    if (matchId && matchId !== presentedMatchId) {
      presentedMatchId = matchId;
      feedbackKey = globalThis.crypto?.randomUUID?.() || '';
      outcomeKey = globalThis.crypto?.randomUUID?.() || '';
      feedbackDialogOpen = true;
      actionError = '';
    }
  });

  $effect(() => {
    const suggestionId = data.selectedSuggestion?.id || '';
    if (suggestionId && suggestionId !== presentedSuggestionId) {
      presentedSuggestionId = suggestionId;
      suggestionKey = globalThis.crypto?.randomUUID?.() || '';
      suggestionDialogOpen = true;
      actionError = '';
    }
  });

  $effect(() => {
    const type = data.filters.type || 'employment';
    if (type === presentedGenerateType && generateKey) return;
    presentedGenerateType = type;
    generateKey = globalThis.crypto?.randomUUID?.() || '';
  });

  /** @param {string} value */
  function label(value) {
    return String(value || '')
      .replaceAll('_', ' ')
      .replace(/\b\w/g, (character) => character.toUpperCase());
  }

  /** @param {string} value */
  function formatDate(value) {
    if (!value) return '—';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return '—';
    return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium' }).format(date);
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
    updateQuery({ type: null, queue: null, match: null, suggestion: null });
  }

  /** @param {any} item */
  function openFeedback(item) {
    updateQuery({ match: item.matchId, suggestion: null });
  }

  function closeFeedback() {
    feedbackDialogOpen = false;
    presentedMatchId = '';
    if (data.filters.match) updateQuery({ match: null });
  }

  /** @param {any} suggestion */
  function openSuggestion(suggestion) {
    updateQuery({ suggestion: suggestion.id, match: null });
  }

  function closeSuggestion() {
    suggestionDialogOpen = false;
    presentedSuggestionId = '';
    if (data.filters.suggestion) updateQuery({ suggestion: null });
  }

  /** @param {any} version */
  function openPublish(version) {
    if (!canCalibrate || busyAction) return;
    publishVersion = version;
    publishKey = globalThis.crypto?.randomUUID?.() || '';
    actionError = '';
    publishDialogOpen = true;
  }

  /**
   * @param {string} action
   * @param {string} successMessage
   * @param {() => void} [onSuccess]
   */
  function mutationEnhance(action, successMessage, onSuccess) {
    return ({ cancel }) => {
      if (busyAction) {
        cancel();
        return;
      }
      busyAction = action;
      actionError = '';
      liveMessage = `${successMessage} in progress.`;
      return async ({ result, update }) => {
        busyAction = '';
        const actionData = /** @type {any} */ (result).data;
        if (result.type === 'success' && actionData?.feedbackUpdated) {
          liveMessage = successMessage;
          toast.success(successMessage);
          onSuccess?.();
          await update({ reset: false, invalidateAll: true });
          return;
        }
        if (result.type === 'failure' && result.status === 403) {
          permissionRevoked = true;
          feedbackDialogOpen = false;
          suggestionDialogOpen = false;
          publishDialogOpen = false;
        }
        actionError = actionData?.actionError || 'The feedback action could not be completed.';
        liveMessage = actionError;
        toast.error(actionError);
        await update({ reset: false, invalidateAll: result.status === 409 });
      };
    };
  }

  const feedbackEnhance = mutationEnhance('feedback', 'Matching feedback saved.', () => {
    feedbackKey = globalThis.crypto?.randomUUID?.() || '';
  });
  const outcomeEnhance = mutationEnhance('outcome', 'Lifecycle outcome recorded.', () => {
    outcomeKey = globalThis.crypto?.randomUUID?.() || '';
  });
  const suggestionEnhance = mutationEnhance('suggestion', 'AI suggestion review saved.', () => {
    closeSuggestion();
  });
  const generateEnhance = mutationEnhance('generate', 'AI weight suggestion generated.', () => {
    generateKey = globalThis.crypto?.randomUUID?.() || '';
  });
  const publishEnhance = mutationEnhance('publish', 'Scoring policy version published.', () => {
    publishDialogOpen = false;
    publishVersion = null;
  });
</script>

<svelte:head><title>Feedback loop · Matching</title></svelte:head>

<div class="flex min-h-0 flex-1 flex-col">
  <PageHeader
    title="Matching feedback loop"
    subtitle="Turn structured human decisions and observable outcomes into reviewable calibration"
    breadcrumb={[{ label: 'Matching', href: '/matching' }, { label: 'Feedback' }]}
  >
    {#snippet actions()}
      <Button href="/matching" size="sm" variant="outline">
        <ArrowLeft class="size-3.5" />Back to matching
      </Button>
      <Button href="/matching/governance" size="sm" variant="outline">
        <ShieldCheck class="size-3.5" />Evidence governance
      </Button>
      {#if canCalibrate}
        <form method="POST" action="?/generateSuggestions" use:enhance={generateEnhance}>
          <input type="hidden" name="opportunity_type" value={data.filters.type || 'employment'} />
          <input
            type="hidden"
            name="expected_revision"
            value={data.weightVersions.find(
              (item) => item.opportunityType === (data.filters.type || 'employment')
            )?.policyRevision || 0}
          />
          <input type="hidden" name="idempotency_key" value={generateKey} />
          <Button type="submit" size="sm" disabled={Boolean(busyAction) || !generateKey}>
            {#if busyAction === 'generate'}<Loader2 class="size-3.5 animate-spin" />{:else}<Sparkles
                class="size-3.5"
              />{/if}
            Generate suggestion
          </Button>
        </form>
      {/if}
    {/snippet}
  </PageHeader>

  <FilterStrip>
    <SelectFilter
      options={typeOptions}
      value={data.filters.type || 'ALL'}
      allLabel="All opportunity types"
      onchange={(value) => updateQuery({ type: String(value), match: null, suggestion: null })}
    />
    <SelectFilter
      options={queueOptions}
      value={data.filters.queue || 'ALL'}
      allLabel="All feedback states"
      onchange={(value) => updateQuery({ queue: String(value), match: null })}
    />
    {#if activeFilterCount > 0}
      <FilterPill label="Clear all" dashed onclick={clearFilters} />
    {/if}
    {#snippet meta()}<span>{data.queueCount} matches</span>{/snippet}
  </FilterStrip>

  <p class="sr-only" role="status" aria-live="polite">{liveMessage}</p>

  <main class="flex-1 space-y-6 px-5 py-5 md:px-8">
    {#if permissionRevoked}
      <div
        role="alert"
        class="rounded-lg border border-[color:var(--amber)] bg-[color:var(--amber-soft)] px-4 py-3 text-sm text-[color:var(--amber-soft-text)]"
      >
        Your matching permissions changed. Read-only data will refresh, but write controls are now
        disabled.
      </div>
    {/if}
    {#if actionError}
      <div
        role="alert"
        class="rounded-lg border border-[color:var(--red)] bg-[color:var(--red-soft)] px-4 py-3 text-sm text-[color:var(--red-soft-text)]"
      >
        {actionError}
      </div>
    {/if}

    <section aria-label="Feedback summary" class="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
      {#each summaryCards as card (card.label)}
        <article
          class="rounded-xl border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-4"
        >
          <div class="flex items-center justify-between gap-2">
            <p class="text-xs font-medium text-[color:var(--text-muted)]">{card.label}</p>
            <card.icon class="size-4 text-[color:var(--violet-soft-text)]" aria-hidden="true" />
          </div>
          <p class="mt-3 text-2xl font-semibold tabular-nums">{card.value}</p>
          <p class="mt-1 text-[11px] text-[color:var(--text-subtle)]">{card.detail}</p>
        </article>
      {/each}
    </section>

    <aside
      class="rounded-xl border border-[color:var(--violet)] bg-[color:var(--violet-soft)] px-4 py-3 text-sm text-[color:var(--violet-soft-text)]"
      aria-label="Feedback privacy and interpretation notice"
    >
      Feedback uses bounded codes and safe evidence summaries. It never displays identities, raw
      conversations, source locators, provider payloads or model prompts. Aggregate relationships
      below are observational and do not establish causation.
    </aside>

    <section
      class="overflow-hidden rounded-xl border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)]"
      aria-labelledby="feedback-queue-title"
    >
      <div class="border-b border-[color:var(--border-faint)] px-5 py-4">
        <h2 id="feedback-queue-title" class="font-semibold">Feedback queue</h2>
        <p class="mt-1 text-xs text-[color:var(--text-subtle)]">
          Record whether recommendations were accurate and what observable result followed.
        </p>
      </div>
      {#if data.queue.length === 0}
        <div class="px-5 py-12 text-center">
          <CheckCircle2 class="mx-auto size-8 text-[color:var(--text-subtle)]" />
          <p class="mt-3 text-sm font-medium">No matches in this feedback queue</p>
          <p class="mt-1 text-xs text-[color:var(--text-subtle)]">
            Change a filter to review another cohort.
          </p>
        </div>
      {:else}
        <div class="hidden overflow-x-auto md:block">
          <table class="w-full text-left text-sm">
            <thead class="bg-[color:var(--bg-subtle)] text-xs text-[color:var(--text-muted)]">
              <tr>
                <th class="px-5 py-3 font-medium">Person</th>
                <th class="px-4 py-3 font-medium">Opportunity</th>
                <th class="px-4 py-3 font-medium">Recommendation</th>
                <th class="px-4 py-3 font-medium">Feedback</th>
                <th class="px-4 py-3"><span class="sr-only">Open</span></th>
              </tr>
            </thead>
            <tbody>
              {#each data.queue as item (item.matchId)}
                <tr class="border-t border-[color:var(--border-faint)] align-top">
                  <td class="px-5 py-4">
                    <p class="font-medium">{item.person.displayName}</p>
                    <p class="mt-1 text-xs text-[color:var(--text-subtle)]">
                      {[item.person.currentTitle, item.person.currentCompany]
                        .filter(Boolean)
                        .join(' · ') || 'No role summary'}
                    </p>
                  </td>
                  <td class="px-4 py-4">
                    <p>{item.opportunity.title}</p>
                    <p class="mt-1 text-xs text-[color:var(--text-subtle)]">
                      {label(item.opportunity.type)}
                    </p>
                  </td>
                  <td class="px-4 py-4">
                    <p class="font-semibold tabular-nums">{item.overallScore}/100</p>
                    <Badge variant="outline">{label(item.status)}</Badge>
                  </td>
                  <td class="px-4 py-4">
                    <Badge variant="neutral">{label(item.accuracy)}</Badge>
                    {#if item.dueFlags.length > 0}
                      <p class="mt-2 text-xs text-[color:var(--amber-soft-text)]">
                        {item.dueFlags.map(label).join(' · ')}
                      </p>
                    {/if}
                  </td>
                  <td class="px-4 py-4 text-right">
                    <Button
                      type="button"
                      size="icon-sm"
                      variant="ghost"
                      aria-label="Review feedback for {item.person.displayName}"
                      onclick={() => openFeedback(item)}
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
          {#each data.queue as item (item.matchId)}
            <li>
              <button
                type="button"
                class="w-full px-4 py-4 text-left focus-visible:ring-2 focus-visible:ring-[color:var(--ring)] focus-visible:outline-none"
                onclick={() => openFeedback(item)}
              >
                <span class="flex items-start justify-between gap-3">
                  <span>
                    <span class="block font-medium">{item.person.displayName}</span>
                    <span class="mt-1 block text-xs text-[color:var(--text-subtle)]"
                      >{item.opportunity.title}</span
                    >
                  </span>
                  <span class="font-semibold tabular-nums">{item.overallScore}</span>
                </span>
                <span class="mt-3 flex flex-wrap gap-2">
                  <Badge variant="outline">{label(item.status)}</Badge>
                  <Badge variant="neutral">{label(item.accuracy)}</Badge>
                </span>
              </button>
            </li>
          {/each}
        </ul>
      {/if}
    </section>

    <section class="grid gap-5 xl:grid-cols-[1.2fr_0.8fr]">
      <article
        class="rounded-xl border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-5"
      >
        <div class="flex items-start justify-between gap-3">
          <div>
            <h2 class="font-semibold">Evidence dimension observations</h2>
            <p class="mt-1 text-xs text-[color:var(--text-subtle)]">
              Aggregated human usefulness feedback; observational, not causal.
            </p>
          </div>
          <Badge variant="outline">Privacy threshold enforced</Badge>
        </div>
        <div class="mt-4 space-y-4">
          {#each EVIDENCE_DIMENSIONS as dimension (dimension)}
            {@const impact = data.overview.evidenceImpact.find(
              (item) => item.dimension === dimension
            )}
            <div class="rounded-lg border border-[color:var(--border-faint)] p-3">
              <div class="flex items-center justify-between gap-3">
                <p class="text-sm font-medium">{label(dimension)}</p>
                <span class="text-xs text-[color:var(--text-subtle)]">
                  {impact?.sampleSize || 0} observations
                </span>
              </div>
              {#if !impact || impact.suppressed}
                <p class="mt-2 flex items-center gap-2 text-xs text-[color:var(--amber-soft-text)]">
                  <AlertTriangle class="size-3.5" />Suppressed because the cohort is below the
                  privacy threshold.
                </p>
              {:else}
                <div class="mt-3">
                  <div class="mb-1 flex justify-between text-xs">
                    <span>Rated helpful</span>
                    <span class="tabular-nums">{Math.round(impact.helpfulRate * 100)}%</span>
                  </div>
                  <Progress
                    class="h-2"
                    value={impact.helpfulRate * 100}
                    max={100}
                    aria-label="{label(dimension)} helpful rate"
                  />
                  <p class="mt-2 text-[11px] text-[color:var(--text-subtle)]">
                    {impact.concernCount} misleading or outdated ratings. These counts are descriptive
                    and are not an estimate of causal impact.
                  </p>
                </div>
              {/if}
            </div>
          {/each}
        </div>
      </article>

      <article
        class="rounded-xl border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-5"
      >
        <h2 class="font-semibold">Recommendation verdicts</h2>
        <p class="mt-1 text-xs text-[color:var(--text-subtle)]">
          Structured verdicts only; free-form notes are excluded.
        </p>
        {#if data.overview.summary.feedbackCaptured > 0}
          <ol class="mt-4 space-y-3">
            {#each Object.entries(data.overview.verdicts).filter(([code]) => code !== 'unknown') as [code, count] (code)}
              <li class="flex items-center justify-between gap-3 text-sm">
                <span>{label(code)}</span>
                <span class="font-medium tabular-nums">{count}</span>
              </li>
            {/each}
          </ol>
        {:else}
          <p class="mt-4 text-sm text-[color:var(--text-muted)]">No recommendation feedback yet.</p>
        {/if}
      </article>
    </section>

    <section class="grid gap-5 xl:grid-cols-2">
      <article
        class="rounded-xl border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-5"
      >
        <div class="flex items-start justify-between gap-3">
          <div>
            <h2 class="flex items-center gap-2 font-semibold">
              <History class="size-4" />Weight versions
            </h2>
            <p class="mt-1 text-xs text-[color:var(--text-subtle)]">
              Immutable versions by opportunity type.
            </p>
          </div>
        </div>
        {#if data.weightVersions.length > 0}
          <ol class="mt-4 space-y-3">
            {#each data.weightVersions as version (version.id)}
              <li class="rounded-lg border border-[color:var(--border-faint)] p-4">
                <div class="flex flex-wrap items-center justify-between gap-3">
                  <div class="flex flex-wrap items-center gap-2">
                    <Badge variant="neutral">{label(version.opportunityType)}</Badge>
                    <span class="text-sm font-semibold">Version {version.version}</span>
                    <Badge variant="outline">{label(version.status)}</Badge>
                  </div>
                  {#if canCalibrate && version.status === 'draft' && version.source === 'human'}
                    <Button size="sm" variant="outline" onclick={() => openPublish(version)}
                      >Publish</Button
                    >
                  {/if}
                </div>
                <dl class="mt-3 grid grid-cols-2 gap-2 text-xs sm:grid-cols-4">
                  {#each SCORING_DIMENSIONS as dimension (dimension)}
                    <div class="rounded bg-[color:var(--bg-subtle)] p-2">
                      <dt class="text-[color:var(--text-subtle)]">{label(dimension)}</dt>
                      <dd class="mt-1 font-semibold tabular-nums">{version.weights[dimension]}</dd>
                    </div>
                  {/each}
                </dl>
              </li>
            {/each}
          </ol>
        {:else}
          <p class="mt-4 text-sm text-[color:var(--text-muted)]">
            No scoring policy versions are available.
          </p>
        {/if}
      </article>

      <article
        class="rounded-xl border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-5"
      >
        <div>
          <h2 class="flex items-center gap-2 font-semibold">
            <Sparkles class="size-4" />AI suggestions
          </h2>
          <p class="mt-1 text-xs text-[color:var(--text-subtle)]">
            Suggestions are never activated automatically. Acceptance creates a draft only.
          </p>
        </div>
        {#if data.suggestions.length > 0}
          <ol class="mt-4 space-y-3">
            {#each data.suggestions as suggestion (suggestion.id)}
              <li class="rounded-lg border border-[color:var(--border-faint)] p-4">
                <div class="flex items-start justify-between gap-3">
                  <div>
                    <div class="flex flex-wrap items-center gap-2">
                      <Badge variant="neutral">{label(suggestion.opportunityType)}</Badge>
                      <Badge variant="outline">{label(suggestion.status)}</Badge>
                    </div>
                    <p class="mt-2 text-xs text-[color:var(--text-subtle)]">
                      {suggestion.sampleSize} observations · human review required
                    </p>
                  </div>
                  <Button size="sm" variant="outline" onclick={() => openSuggestion(suggestion)}>
                    Review
                  </Button>
                </div>
              </li>
            {/each}
          </ol>
        {:else}
          <p class="mt-4 text-sm text-[color:var(--text-muted)]">No reviewable AI suggestions.</p>
        {/if}
      </article>
    </section>
  </main>
</div>

<MatchFeedbackDialog
  bind:open={feedbackDialogOpen}
  detail={data.selected}
  {canFeedback}
  {busyAction}
  {actionError}
  {feedbackKey}
  {outcomeKey}
  {feedbackEnhance}
  {outcomeEnhance}
  onClose={closeFeedback}
/>

<WeightSuggestionReviewDialog
  bind:open={suggestionDialogOpen}
  suggestion={data.selectedSuggestion}
  {canCalibrate}
  busy={busyAction === 'suggestion'}
  {actionError}
  idempotencyKey={suggestionKey}
  reviewEnhance={suggestionEnhance}
  onClose={closeSuggestion}
/>

<AlertDialog.Root bind:open={publishDialogOpen}>
  <AlertDialog.Content class="max-w-md">
    <AlertDialog.Header>
      <AlertDialog.Title>Publish scoring policy version?</AlertDialog.Title>
      <AlertDialog.Description>
        Publishing makes version {publishVersion?.version || '—'} active for future recomputations. Existing
        human decisions are preserved. This requires a separate explicit confirmation from accepting an
        AI suggestion.
      </AlertDialog.Description>
    </AlertDialog.Header>
    {#if publishVersion}
      <form method="POST" action="?/publishPolicy" use:enhance={publishEnhance}>
        <input type="hidden" name="version_id" value={publishVersion.id} />
        <input type="hidden" name="expected_revision" value={publishVersion.policyRevision} />
        <input type="hidden" name="idempotency_key" value={publishKey} />
        <AlertDialog.Footer>
          <AlertDialog.Cancel disabled={Boolean(busyAction)}>Keep draft</AlertDialog.Cancel>
          <Button type="submit" disabled={!canCalibrate || Boolean(busyAction) || !publishKey}>
            {#if busyAction === 'publish'}<Loader2 class="size-3.5 animate-spin" />{/if}
            Publish version
          </Button>
        </AlertDialog.Footer>
      </form>
    {/if}
  </AlertDialog.Content>
</AlertDialog.Root>
