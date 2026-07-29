<script>
  import { enhance } from '$app/forms';
  import { Button } from '$lib/components/ui/button/index.js';
  import { Input } from '$lib/components/ui/input/index.js';
  import { Label } from '$lib/components/ui/label/index.js';
  import { PageHeader } from '$lib/components/layout';
  import { toast } from 'svelte-sonner';
  import { AlertTriangle, Bot, CheckCircle2, Globe2, Sparkles, WandSparkles } from '@lucide/svelte';

  /** @type {{ data: any, form: any }} */
  let { data, form } = $props();

  const configuration = $derived(data.configuration);
  const inspections = $derived(data.inspections?.results || []);
  const completedCount = $derived(inspections.filter((item) => item.status === 'completed').length);
  const fallbackCount = $derived(inspections.filter((item) => item.used_fallback).length);
  const averageScore = $derived(
    inspections.length
      ? Math.round(
          inspections.reduce((total, item) => total + (item.qualification_score || 0), 0) /
            inspections.length
        )
      : 0
  );

  $effect(() => {
    if (form?.actionError) toast.error(form.actionError);
    if (form?.saved) toast.success('AI lead inspector settings saved.');
  });

  /** @param {string} status */
  function statusClass(status) {
    if (status === 'completed') return 'bg-emerald-100 text-emerald-700';
    if (status === 'failed') return 'bg-red-100 text-red-700';
    if (status === 'partial') return 'bg-amber-100 text-amber-700';
    return 'bg-blue-100 text-blue-700';
  }

  /** @param {string} band */
  function bandClass(band) {
    if (band === 'high') return 'text-emerald-600';
    if (band === 'medium') return 'text-blue-600';
    if (band === 'low') return 'text-amber-600';
    return 'text-red-600';
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
  <title>AI Lead Inspector - BottleCRM</title>
</svelte:head>

<PageHeader
  title="AI Lead Inspector"
  subtitle="Research company websites, qualify leads against each tenant's ICP, and retain an audit trail"
/>

<div class="space-y-6 p-6 md:p-8">
  {#if !configuration.openai_configured}
    <div class="flex gap-3 rounded-lg border border-amber-200 bg-amber-50 p-4 text-amber-900">
      <AlertTriangle class="mt-0.5 size-5 shrink-0" />
      <div>
        <p class="text-sm font-medium">OpenAI is not configured on this deployment</p>
        <p class="mt-1 text-xs">
          Leads will still be created and scored with deterministic rules. Add OPENAI_API_KEY to the
          backend environment to enable AI scoring.
        </p>
      </div>
    </div>
  {/if}

  <div class="grid gap-4 sm:grid-cols-4">
    <div
      class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-4"
    >
      <p class="text-xs text-[color:var(--text-muted)]">Inspector</p>
      <p
        class="mt-2 text-lg font-semibold {configuration.is_enabled
          ? 'text-emerald-600'
          : 'text-slate-500'}"
      >
        {configuration.is_enabled ? 'Enabled' : 'Disabled'}
      </p>
    </div>
    <div
      class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-4"
    >
      <p class="text-xs text-[color:var(--text-muted)]">Recent inspections</p>
      <p class="mt-2 text-2xl font-semibold text-[color:var(--text-primary)]">
        {inspections.length}
      </p>
    </div>
    <div
      class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-4"
    >
      <p class="text-xs text-[color:var(--text-muted)]">Completed / fallback</p>
      <p class="mt-2 text-2xl font-semibold text-[color:var(--text-primary)]">
        {completedCount} / {fallbackCount}
      </p>
    </div>
    <div
      class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-4"
    >
      <p class="text-xs text-[color:var(--text-muted)]">Average score</p>
      <p class="mt-2 text-2xl font-semibold text-[color:var(--text-primary)]">{averageScore}</p>
    </div>
  </div>

  <section
    class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-5"
  >
    <div class="mb-5 flex items-start gap-3">
      <WandSparkles class="mt-0.5 size-5 text-violet-500" />
      <div>
        <h2 class="font-medium text-[color:var(--text-primary)]">Inspector configuration</h2>
        <p class="text-xs text-[color:var(--text-muted)]">
          These instructions apply only to the current organization.
        </p>
      </div>
    </div>

    <form method="POST" action="?/save" use:enhance class="space-y-5">
      <div class="grid gap-3 sm:grid-cols-3">
        <label
          class="flex items-start gap-2 rounded-md border border-[color:var(--border-faint)] p-3 text-sm"
        >
          <input
            type="checkbox"
            name="is_enabled"
            checked={configuration.is_enabled}
            class="mt-0.5"
          />
          <span
            ><strong class="block">Enable inspector</strong><span
              class="text-xs text-[color:var(--text-muted)]"
              >Apply research and configured scoring to new leads.</span
            ></span
          >
        </label>
        <label
          class="flex items-start gap-2 rounded-md border border-[color:var(--border-faint)] p-3 text-sm"
        >
          <input
            type="checkbox"
            name="research_enabled"
            checked={configuration.research_enabled}
            class="mt-0.5"
          />
          <span
            ><strong class="block">Website research</strong><span
              class="text-xs text-[color:var(--text-muted)]"
              >Read a small number of public company pages.</span
            ></span
          >
        </label>
        <label
          class="flex items-start gap-2 rounded-md border border-[color:var(--border-faint)] p-3 text-sm"
        >
          <input
            type="checkbox"
            name="ai_scoring_enabled"
            checked={configuration.ai_scoring_enabled}
            class="mt-0.5"
          />
          <span
            ><strong class="block">AI qualification</strong><span
              class="text-xs text-[color:var(--text-muted)]"
              >Use the tenant ICP and strict structured output.</span
            ></span
          >
        </label>
      </div>

      <div class="grid gap-4 sm:grid-cols-4">
        <div class="sm:col-span-2">
          <Label for="ai-model">OpenAI model</Label><select
            id="ai-model"
            name="model"
            value={configuration.model}
            class="h-9 w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] px-3 text-sm"
          >
            {#each configuration.allowed_models as model (model)}
              <option value={model}>{model}</option>
            {/each}
          </select>
        </div>
        <div>
          <Label for="reasoning-effort">Reasoning</Label><select
            id="reasoning-effort"
            name="reasoning_effort"
            value={configuration.reasoning_effort}
            class="h-9 w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] px-3 text-sm"
          >
            {#each configuration.allowed_reasoning_efforts as effort (effort)}
              <option value={effort}>{effort === 'none' ? 'None' : effort}</option>
            {/each}
          </select>
        </div>
        <div class="grid grid-cols-2 gap-2">
          <div>
            <Label for="max-pages">Max pages</Label><Input
              id="max-pages"
              name="max_research_pages"
              type="number"
              min="1"
              max="3"
              value={configuration.max_research_pages}
            />
          </div>
          <div>
            <Label for="web-timeout">Timeout</Label><Input
              id="web-timeout"
              name="website_timeout_seconds"
              type="number"
              min="1"
              max="15"
              value={configuration.website_timeout_seconds}
            />
          </div>
        </div>
      </div>

      <div>
        <Label for="icp-description">Ideal customer profile</Label><textarea
          id="icp-description"
          name="icp_description"
          rows="5"
          maxlength="5000"
          class="mt-1 w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-3 text-sm"
          placeholder="Describe the industries, company sizes, countries, buyer roles, and use cases that make a strong customer."
          >{configuration.icp_description}</textarea
        >
      </div>
      <div class="grid gap-4 sm:grid-cols-2">
        <div>
          <Label for="positive-signals">Positive signals</Label><textarea
            id="positive-signals"
            name="positive_signals"
            rows="4"
            maxlength="5000"
            class="mt-1 w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-3 text-sm"
            placeholder="Examples: multi-location operation, VP-level buyer, urgent automation project"
            >{configuration.positive_signals}</textarea
          >
        </div>
        <div>
          <Label for="negative-signals">Negative or disqualifying signals</Label><textarea
            id="negative-signals"
            name="negative_signals"
            rows="4"
            maxlength="5000"
            class="mt-1 w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-3 text-sm"
            placeholder="Examples: student, competitor, unsupported geography, personal project"
            >{configuration.negative_signals}</textarea
          >
        </div>
      </div>
      <div class="flex justify-end">
        <Button type="submit" class="gap-1.5"
          ><Sparkles class="size-4" />Save inspector settings</Button
        >
      </div>
    </form>
  </section>

  <section
    class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)]"
  >
    <div class="flex items-start gap-3 border-b border-[color:var(--border-faint)] p-5">
      <Bot class="mt-0.5 size-5 text-blue-500" />
      <div>
        <h2 class="font-medium text-[color:var(--text-primary)]">Inspection ledger</h2>
        <p class="text-xs text-[color:var(--text-muted)]">
          Research evidence, model usage, fallback, and qualification reasons for recent leads.
        </p>
      </div>
    </div>
    {#if inspections.length}
      <div class="overflow-x-auto">
        <table class="w-full min-w-[1000px] text-left text-sm">
          <thead class="bg-[color:var(--bg-subtle)] text-xs text-[color:var(--text-muted)]"
            ><tr
              ><th class="px-4 py-3 font-medium">Lead</th><th class="px-4 py-3 font-medium"
                >Status</th
              ><th class="px-4 py-3 font-medium">Score</th><th class="px-4 py-3 font-medium"
                >Provider</th
              ><th class="px-4 py-3 font-medium">Evidence</th><th class="px-4 py-3 font-medium"
                >Completed</th
              ></tr
            ></thead
          >
          <tbody class="divide-y divide-[color:var(--border-faint)]">
            {#each inspections as item (item.id)}
              <tr class="align-top">
                <td class="px-4 py-3"
                  ><p class="font-medium text-[color:var(--text-primary)]">
                    {item.company_name || 'Unknown company'}
                  </p>
                  <p class="mt-1 text-xs text-[color:var(--text-muted)]">
                    {item.source} · {item.source_record_id}
                  </p></td
                >
                <td class="px-4 py-3"
                  ><span class="rounded-full px-2 py-0.5 text-[11px] {statusClass(item.status)}"
                    >{item.status}</span
                  >{#if item.used_fallback}<p class="mt-1 text-xs text-amber-600">
                      Rule fallback used
                    </p>{/if}</td
                >
                <td class="px-4 py-3"
                  ><p class="text-lg font-semibold {bandClass(item.qualification_band)}">
                    {item.qualification_score ?? '—'}
                  </p>
                  <p class="text-xs text-[color:var(--text-muted)] capitalize">
                    {item.qualification_band}
                  </p></td
                >
                <td class="px-4 py-3"
                  ><p class="font-medium">{item.provider || '—'}</p>
                  <p class="mt-1 text-xs text-[color:var(--text-muted)]">{item.model || '—'}</p>
                  <p class="text-xs text-[color:var(--text-muted)]">
                    {item.input_tokens ?? 0} in / {item.output_tokens ?? 0} out
                  </p></td
                >
                <td class="max-w-[360px] px-4 py-3"
                  ><p class="line-clamp-2 text-xs text-[color:var(--text-muted)]">
                    {item.research_summary ||
                      item.qualification_reasons?.join(' · ') ||
                      'No website evidence'}
                  </p>
                  {#if item.source_urls?.length}<p
                      class="mt-1 flex items-center gap-1 text-xs text-blue-600"
                    >
                      <Globe2 class="size-3" />{item.source_urls.length} public page(s)
                    </p>{/if}{#if item.error_message}<p class="mt-1 text-xs text-red-600">
                      {item.error_message}
                    </p>{/if}</td
                >
                <td class="px-4 py-3 text-xs text-[color:var(--text-muted)]"
                  >{formatDate(item.completed_at)}</td
                >
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {:else}
      <div class="p-10 text-center">
        <CheckCircle2 class="mx-auto size-8 text-emerald-500" />
        <p class="mt-3 text-sm font-medium">No inspections yet</p>
        <p class="mt-1 text-xs text-[color:var(--text-muted)]">
          Enable the inspector; new inbound leads will appear here.
        </p>
      </div>
    {/if}
  </section>
</div>
