<script>
  import { enhance } from '$app/forms';
  import { AlertTriangle, CheckCircle2, Loader2, Target } from '@lucide/svelte';

  import { Badge } from '$lib/components/ui/badge/index.js';
  import { Button } from '$lib/components/ui/button/index.js';
  import * as Dialog from '$lib/components/ui/dialog/index.js';
  import {
    ACCURACY_LABELS,
    EVIDENCE_ASSESSMENTS,
    EVIDENCE_DIMENSIONS,
    REJECTION_REASON_CODES
  } from '$lib/matching/feedback.js';

  let {
    open = $bindable(false),
    detail = null,
    canFeedback = false,
    busyAction = '',
    actionError = '',
    feedbackKey = '',
    outcomeKey = '',
    feedbackEnhance,
    outcomeEnhance,
    onClose
  } = $props();

  let presentedMatchId = $state('');
  let accuracy = $state('');
  let rejectionReasonCode = $state('');
  let evidenceRatings = $state(
    /** @type {Record<string, {dimension:string,assessment:string}>} */ ({})
  );
  let outcomeCode = $state('');
  let outcomeOccurredOn = $state('');

  const evidenceAssessmentPayload = $derived(
    JSON.stringify(
      Object.entries(evidenceRatings).map(([evidenceId, value]) => ({
        evidence_id: evidenceId,
        dimension: value.dimension,
        assessment: value.assessment
      }))
    )
  );

  $effect(() => {
    const matchId = detail?.match?.id || '';
    if (!open || !matchId || matchId === presentedMatchId) return;
    presentedMatchId = matchId;
    accuracy = '';
    rejectionReasonCode = REJECTION_REASON_CODES.includes(detail.feedback?.rejectionReasonCode)
      ? detail.feedback.rejectionReasonCode
      : '';
    const existing = new Map(
      (detail.feedback?.evidenceAssessments || []).map((item) => [item.evidenceId, item])
    );
    evidenceRatings = Object.fromEntries(
      (detail.match?.evidenceLinks || []).slice(0, 20).map((link) => {
        const saved = existing.get(link.evidence.id);
        return [
          link.evidence.id,
          {
            dimension: saved?.dimension || 'skills',
            assessment: saved?.assessment || 'neutral'
          }
        ];
      })
    );
    outcomeCode = detail.availableOutcomes?.[0]?.code || '';
    outcomeOccurredOn = new Date().toISOString().slice(0, 10);
  });

  /** @param {string} evidenceId @param {'dimension'|'assessment'} field @param {string} value */
  function setEvidenceRating(evidenceId, field, value) {
    const current = evidenceRatings[evidenceId] || { dimension: 'skills', assessment: 'neutral' };
    evidenceRatings = { ...evidenceRatings, [evidenceId]: { ...current, [field]: value } };
  }

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
</script>

<Dialog.Root bind:open onOpenChange={(next) => !next && onClose?.()}>
  <Dialog.Content class="max-h-[92vh] overflow-y-auto sm:max-w-3xl">
    <Dialog.Header>
      <Dialog.Title>Feedback for {detail?.match?.personName || 'candidate'}</Dialog.Title>
      <Dialog.Description>
        Record structured matching feedback. Do not paste messages, identity values or provider
        output.
      </Dialog.Description>
    </Dialog.Header>

    {#if detail?.match}
      <div class="grid gap-3 rounded-lg bg-[color:var(--bg-subtle)] p-4 text-sm sm:grid-cols-3">
        <div>
          <p class="text-xs text-[color:var(--text-subtle)]">Score</p>
          <p class="mt-1 font-semibold tabular-nums">{detail.match.overallScore}/100</p>
        </div>
        <div>
          <p class="text-xs text-[color:var(--text-subtle)]">Decision</p>
          <p class="mt-1 font-semibold">{label(detail.match.status)}</p>
        </div>
        <div>
          <p class="text-xs text-[color:var(--text-subtle)]">Ranking revision</p>
          <p class="mt-1 font-semibold tabular-nums">{detail.match.rankingRevision}</p>
        </div>
      </div>

      {#if actionError}
        <div
          id="matching-feedback-action-error"
          role="alert"
          class="rounded-lg border border-[color:var(--red)] bg-[color:var(--red-soft)] px-4 py-3 text-sm text-[color:var(--red-soft-text)]"
        >
          {actionError}
        </div>
      {/if}

      <form
        method="POST"
        action="?/saveFeedback"
        use:enhance={feedbackEnhance}
        class="space-y-5"
        aria-describedby={actionError ? 'matching-feedback-action-error' : undefined}
      >
        <input type="hidden" name="match_id" value={detail.match.id} />
        <input type="hidden" name="match_status" value={detail.match.status} />
        <input type="hidden" name="expected_revision" value={detail.feedback.revision} />
        <input
          type="hidden"
          name="expected_ranking_revision"
          value={detail.match.rankingRevision}
        />
        <input type="hidden" name="idempotency_key" value={feedbackKey} />
        <input type="hidden" name="evidence_assessments" value={evidenceAssessmentPayload} />

        <fieldset disabled={!canFeedback || Boolean(busyAction)} class="space-y-4">
          <legend class="font-semibold text-[color:var(--text)]">Assessment accuracy</legend>
          <p class="text-xs text-[color:var(--text-subtle)]">
            Judge whether the ranking assessment was useful and accurate at the time it was made.
          </p>
          <label for="feedback-accuracy" class="block text-xs font-medium">
            Accuracy
            <select
              id="feedback-accuracy"
              name="accuracy"
              bind:value={accuracy}
              required
              class="mt-1 w-full rounded-md border bg-transparent px-3 py-2 text-sm"
            >
              <option value="" disabled>Select an assessment</option>
              {#each ACCURACY_LABELS as item (item)}
                <option value={item}>{label(item)}</option>
              {/each}
            </select>
          </label>

          {#if detail.match.status === 'rejected'}
            <label for="feedback-rejection-reason" class="block text-xs font-medium">
              Rejection reason
              <select
                id="feedback-rejection-reason"
                name="rejection_reason_code"
                bind:value={rejectionReasonCode}
                required
                class="mt-1 w-full rounded-md border bg-transparent px-3 py-2 text-sm"
              >
                <option value="" disabled>Select a structured reason</option>
                {#each REJECTION_REASON_CODES as code (code)}
                  <option value={code}>{label(code)}</option>
                {/each}
              </select>
            </label>
          {:else}
            <input type="hidden" name="rejection_reason_code" value="" />
          {/if}
        </fieldset>

        <section aria-labelledby="feedback-evidence-title">
          <div class="flex items-center justify-between gap-3">
            <div>
              <h3 id="feedback-evidence-title" class="font-semibold text-[color:var(--text)]">
                Evidence usefulness
              </h3>
              <p class="mt-1 text-xs text-[color:var(--text-subtle)]">
                This rates usefulness for this match; it does not confirm or reject the underlying
                fact.
              </p>
            </div>
            <Badge variant="outline">{detail.match.evidenceLinks.length}</Badge>
          </div>

          {#if detail.match.evidenceLinks.length > 0}
            <ol class="mt-3 space-y-3">
              {#each detail.match.evidenceLinks.slice(0, 20) as link (link.evidence.id)}
                <li class="rounded-lg border border-[color:var(--border-faint)] p-3">
                  <div class="flex flex-wrap items-center gap-2">
                    <Badge variant="neutral">{label(link.evidence.kind)}</Badge>
                    <Badge variant="outline">{label(link.evidence.source)}</Badge>
                    {#if link.evidence.reviewStatus}
                      <span class="text-[11px] text-[color:var(--text-subtle)]">
                        {label(link.evidence.reviewStatus)}
                      </span>
                    {/if}
                  </div>
                  <p class="mt-2 text-xs leading-5 text-[color:var(--text-muted)]">
                    {link.evidence.summary || 'No bounded evidence summary is available.'}
                  </p>
                  <div class="mt-3 grid gap-3 sm:grid-cols-2">
                    <label class="text-xs font-medium">
                      Dimension
                      <select
                        value={evidenceRatings[link.evidence.id]?.dimension || 'skills'}
                        disabled={!canFeedback || Boolean(busyAction)}
                        onchange={(event) =>
                          setEvidenceRating(
                            link.evidence.id,
                            'dimension',
                            event.currentTarget.value
                          )}
                        class="mt-1 w-full rounded-md border bg-transparent px-2 py-1.5 text-xs"
                      >
                        {#each EVIDENCE_DIMENSIONS as dimension (dimension)}
                          <option value={dimension}>{label(dimension)}</option>
                        {/each}
                      </select>
                    </label>
                    <label class="text-xs font-medium">
                      Usefulness
                      <select
                        value={evidenceRatings[link.evidence.id]?.assessment || 'neutral'}
                        disabled={!canFeedback || Boolean(busyAction)}
                        onchange={(event) =>
                          setEvidenceRating(
                            link.evidence.id,
                            'assessment',
                            event.currentTarget.value
                          )}
                        class="mt-1 w-full rounded-md border bg-transparent px-2 py-1.5 text-xs"
                      >
                        {#each EVIDENCE_ASSESSMENTS as assessment (assessment)}
                          <option value={assessment}>{label(assessment)}</option>
                        {/each}
                      </select>
                    </label>
                  </div>
                </li>
              {/each}
            </ol>
          {:else}
            <p
              class="mt-3 rounded-md bg-[color:var(--bg-subtle)] p-3 text-xs text-[color:var(--text-muted)]"
            >
              No safe evidence summaries are attached to this match.
            </p>
          {/if}
        </section>

        <div class="flex items-center justify-between gap-3 border-t pt-4">
          {#if !canFeedback}
            <p class="text-xs text-[color:var(--text-subtle)]">Feedback permission is required.</p>
          {:else}
            <span></span>
          {/if}
          <Button
            type="submit"
            disabled={!canFeedback || Boolean(busyAction) || !feedbackKey}
            aria-busy={busyAction === 'feedback'}
          >
            {#if busyAction === 'feedback'}<Loader2 class="size-3.5 animate-spin" />{/if}
            Save feedback
          </Button>
        </div>
      </form>

      <section class="space-y-3 border-t pt-5" aria-labelledby="matching-outcome-title">
        <div>
          <h3 id="matching-outcome-title" class="flex items-center gap-2 font-semibold">
            <Target class="size-4" />Milestone outcome
          </h3>
          <p class="mt-1 text-xs text-[color:var(--text-subtle)]">
            Record an observable outcome, not an inference about the person.
          </p>
        </div>

        {#if detail.availableOutcomes.length > 0}
          <form
            method="POST"
            action="?/recordOutcome"
            use:enhance={outcomeEnhance}
            class="space-y-3"
          >
            <input type="hidden" name="match_id" value={detail.match.id} />
            <input type="hidden" name="expected_revision" value={detail.feedbackRevision} />
            <input
              type="hidden"
              name="expected_ranking_revision"
              value={detail.match.rankingRevision}
            />
            <input type="hidden" name="idempotency_key" value={outcomeKey} />
            <div>
              <label for="matching-outcome-code" class="text-xs font-medium">
                Outcome
                <select
                  id="matching-outcome-code"
                  name="outcome_code"
                  bind:value={outcomeCode}
                  required
                  disabled={!canFeedback || Boolean(busyAction)}
                  class="mt-1 w-full rounded-md border bg-transparent px-3 py-2 text-sm"
                >
                  {#each detail.availableOutcomes as item (item.code)}
                    <option value={item.code}>{item.label}</option>
                  {/each}
                </select>
              </label>
            </div>
            <label for="matching-outcome-observed" class="block text-xs font-medium">
              Observed at
              <input
                id="matching-outcome-observed"
                name="observed_at"
                type="date"
                required
                disabled={!canFeedback || Boolean(busyAction)}
                bind:value={outcomeOccurredOn}
                class="mt-1 w-full rounded-md border bg-transparent px-3 py-2 text-sm"
              />
            </label>
            <div class="flex justify-end">
              <Button
                type="submit"
                variant="outline"
                disabled={!canFeedback || Boolean(busyAction) || !outcomeKey}
                aria-busy={busyAction === 'outcome'}
              >
                {#if busyAction === 'outcome'}<Loader2 class="size-3.5 animate-spin" />{/if}
                Record milestone
              </Button>
            </div>
          </form>
        {:else}
          <p
            class="rounded-md bg-[color:var(--bg-subtle)] p-3 text-xs text-[color:var(--text-muted)]"
          >
            This opportunity type has no configured milestones.
          </p>
        {/if}

        {#if detail.outcomes.length > 0}
          <ol class="space-y-2" aria-label="Recorded milestone outcomes">
            {#each detail.outcomes as item (item.id)}
              <li class="flex items-center justify-between gap-3 rounded-md border p-3 text-xs">
                <span class="flex items-center gap-2">
                  {#if ['deal_won', 'hired', 'collaboration_completed', 'referral_accepted'].includes(item.outcomeCode)}
                    <CheckCircle2 class="size-3.5 text-[color:var(--green-soft-text)]" />
                  {:else}
                    <AlertTriangle class="size-3.5 text-[color:var(--amber-soft-text)]" />
                  {/if}
                  {label(item.outcomeCode)}
                </span>
                <span class="text-right text-[color:var(--text-subtle)]">
                  {formatDate(item.occurredAt)}
                </span>
              </li>
            {/each}
          </ol>
        {/if}
      </section>
    {:else}
      <p class="py-8 text-center text-sm text-[color:var(--text-muted)]">
        Feedback detail could not be loaded safely.
      </p>
    {/if}

    <Dialog.Footer>
      <Button
        type="button"
        variant="outline"
        disabled={Boolean(busyAction)}
        onclick={() => onClose?.()}
      >
        Close
      </Button>
    </Dialog.Footer>
  </Dialog.Content>
</Dialog.Root>
