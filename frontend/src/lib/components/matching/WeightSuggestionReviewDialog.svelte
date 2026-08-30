<script>
  import { enhance } from '$app/forms';
  import { AlertTriangle, ArrowRight, Loader2, Sparkles } from '@lucide/svelte';

  import { Badge } from '$lib/components/ui/badge/index.js';
  import { Button } from '$lib/components/ui/button/index.js';
  import * as Dialog from '$lib/components/ui/dialog/index.js';
  import { SCORING_DIMENSIONS } from '$lib/matching/feedback.js';

  let {
    open = $bindable(false),
    suggestion = null,
    canCalibrate = false,
    busy = false,
    actionError = '',
    idempotencyKey = '',
    reviewEnhance,
    onClose
  } = $props();

  let reasonCode = $state('aggregate_evidence_reviewed');
  const reviewable = $derived(canCalibrate && suggestion?.status === 'pending');

  $effect(() => {
    if (open) reasonCode = 'aggregate_evidence_reviewed';
  });

  /** @param {string} value */
  function label(value) {
    return String(value || '')
      .replaceAll('_', ' ')
      .replace(/\b\w/g, (character) => character.toUpperCase());
  }
</script>

<Dialog.Root bind:open onOpenChange={(next) => !next && onClose?.()}>
  <Dialog.Content class="max-h-[92vh] overflow-y-auto sm:max-w-2xl">
    <Dialog.Header>
      <Dialog.Title>Review AI weight suggestion</Dialog.Title>
      <Dialog.Description>
        AI suggestions use aggregate feedback only. Accepting creates a draft; it never changes the
        active ranking policy.
      </Dialog.Description>
    </Dialog.Header>

    {#if suggestion}
      <div class="flex flex-wrap items-center gap-2">
        <Badge variant="neutral">{label(suggestion.opportunityType)}</Badge>
        <Badge variant="outline">{label(suggestion.status)}</Badge>
        <span class="text-xs text-[color:var(--text-subtle)]">
          {suggestion.sampleSize} aggregate observations · observational signal only
        </span>
      </div>

      <section aria-labelledby="weight-comparison-title">
        <h3 id="weight-comparison-title" class="font-semibold text-[color:var(--text)]">
          Weight comparison
        </h3>
        <div class="mt-3 overflow-x-auto rounded-lg border border-[color:var(--border-faint)]">
          <table class="w-full min-w-[420px] text-left text-sm">
            <thead class="bg-[color:var(--bg-subtle)] text-xs text-[color:var(--text-muted)]">
              <tr>
                <th class="px-4 py-2 font-medium">Dimension</th>
                <th class="px-4 py-2 text-right font-medium">Current</th>
                <th class="px-4 py-2"><span class="sr-only">Change</span></th>
                <th class="px-4 py-2 text-right font-medium">Proposed</th>
              </tr>
            </thead>
            <tbody>
              {#each SCORING_DIMENSIONS as dimension (dimension)}
                <tr class="border-t border-[color:var(--border-faint)]">
                  <th class="px-4 py-3 font-medium">{label(dimension)}</th>
                  <td class="px-4 py-3 text-right tabular-nums">
                    {suggestion.currentWeights[dimension]}
                  </td>
                  <td class="px-4 py-3 text-center">
                    <ArrowRight class="inline size-3.5" aria-hidden="true" />
                  </td>
                  <td class="px-4 py-3 text-right font-semibold tabular-nums">
                    {suggestion.proposedWeights[dimension]}
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      </section>

      <section class="grid gap-3 sm:grid-cols-2" aria-label="Suggestion rationale and warnings">
        <div class="rounded-lg bg-[color:var(--violet-soft)] p-4 text-sm">
          <h3 class="flex items-center gap-2 font-semibold text-[color:var(--violet-soft-text)]">
            <Sparkles class="size-4" />Aggregate rationale
          </h3>
          {#if suggestion.rationaleCodes.length > 0}
            <ul class="mt-2 list-disc space-y-1 pl-5 text-xs text-[color:var(--violet-soft-text)]">
              {#each suggestion.rationaleCodes as code (code)}
                <li>{label(code)}</li>
              {/each}
            </ul>
          {:else}
            <p class="mt-2 text-xs">No bounded rationale codes were provided.</p>
          {/if}
        </div>
        <div class="rounded-lg bg-[color:var(--amber-soft)] p-4 text-sm">
          <h3 class="flex items-center gap-2 font-semibold text-[color:var(--amber-soft-text)]">
            <AlertTriangle class="size-4" />Guardrails
          </h3>
          {#if suggestion.warningCodes.length > 0}
            <ul class="mt-2 list-disc space-y-1 pl-5 text-xs text-[color:var(--amber-soft-text)]">
              {#each suggestion.warningCodes as code (code)}
                <li>{label(code)}</li>
              {/each}
            </ul>
          {:else}
            <p class="mt-2 text-xs">No bounded warning codes were reported.</p>
          {/if}
        </div>
      </section>

      {#if actionError}
        <div
          id="weight-suggestion-action-error"
          role="alert"
          class="rounded-lg border border-[color:var(--red)] bg-[color:var(--red-soft)] px-4 py-3 text-sm text-[color:var(--red-soft-text)]"
        >
          {actionError}
        </div>
      {/if}

      <form
        method="POST"
        action="?/reviewSuggestion"
        use:enhance={reviewEnhance}
        class="space-y-4"
        aria-describedby={actionError ? 'weight-suggestion-action-error' : undefined}
      >
        <input type="hidden" name="suggestion_id" value={suggestion.id} />
        <input type="hidden" name="expected_revision" value={suggestion.revision} />
        <input type="hidden" name="idempotency_key" value={idempotencyKey} />
        <label for="suggestion-review-reason" class="block text-xs font-medium">
          Audit reason
          <select
            id="suggestion-review-reason"
            name="reason_code"
            bind:value={reasonCode}
            required
            disabled={!reviewable || busy}
            class="mt-1 w-full rounded-md border bg-transparent px-3 py-2 text-sm"
          >
            <option value="aggregate_evidence_reviewed">Aggregate evidence reviewed</option>
            <option value="insufficient_sample">Insufficient sample</option>
            <option value="policy_risk">Policy risk</option>
            <option value="business_override">Business override</option>
          </select>
        </label>
        <p
          class="rounded-md bg-[color:var(--bg-subtle)] p-3 text-xs text-[color:var(--text-subtle)]"
        >
          Prompt text, model output and provider payloads are intentionally not available in this
          interface.
        </p>
        <Dialog.Footer>
          <Button type="button" variant="outline" disabled={busy} onclick={() => onClose?.()}>
            Cancel
          </Button>
          <Button
            type="submit"
            name="review_action"
            value="reject"
            variant="destructive"
            disabled={!reviewable || busy || !idempotencyKey}
          >
            Reject
          </Button>
          <Button
            type="submit"
            name="review_action"
            value="accept"
            disabled={!reviewable || busy || !idempotencyKey}
            aria-busy={busy}
          >
            {#if busy}<Loader2 class="size-3.5 animate-spin" />{/if}
            Accept as draft
          </Button>
        </Dialog.Footer>
      </form>
    {:else}
      <p class="py-8 text-center text-sm text-[color:var(--text-muted)]">
        Suggestion detail could not be loaded safely.
      </p>
    {/if}
  </Dialog.Content>
</Dialog.Root>
