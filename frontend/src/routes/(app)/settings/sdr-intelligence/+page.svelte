<script>
  import { enhance } from '$app/forms';
  import { Button } from '$lib/components/ui/button/index.js';
  import { Input } from '$lib/components/ui/input/index.js';
  import { Label } from '$lib/components/ui/label/index.js';
  import { PageHeader } from '$lib/components/layout';
  import { toast } from 'svelte-sonner';
  import {
    AlertTriangle,
    Bot,
    CheckCircle2,
    Globe2,
    KeyRound,
    Network,
    Sparkles,
    WandSparkles
  } from '@lucide/svelte';

  /** @type {{ data: any, form: any }} */
  let { data, form } = $props();

  const configuration = $derived(data.configuration);
  const catalog = $derived(configuration.provider_catalog || {});
  const providerEntries = $derived(Object.entries(catalog));
  const inspections = $derived(data.inspections?.results || []);
  const audits = $derived(Array.isArray(data.audits) ? data.audits : data.audits?.results || []);
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

  /** @param {string} field @param {string} fallback */
  function initialRouteValue(field, fallback = '') {
    return data.configuration?.[field] || fallback;
  }

  let selectedProvider = $state(initialRouteValue('provider', 'openai'));
  let selectedModel = $state(initialRouteValue('model'));
  let selectedFallbackProvider = $state(initialRouteValue('fallback_provider'));
  let selectedFallbackModel = $state(initialRouteValue('fallback_model'));

  const primaryModels = $derived(catalog[selectedProvider]?.models || []);
  const fallbackModels = $derived(catalog[selectedFallbackProvider]?.models || []);
  const primaryProviderStatus = $derived(catalog[selectedProvider] || {});

  $effect(() => {
    selectedProvider = data.configuration.provider || 'openai';
    selectedModel = data.configuration.model || '';
    selectedFallbackProvider = data.configuration.fallback_provider || '';
    selectedFallbackModel = data.configuration.fallback_model || '';
  });

  $effect(() => {
    if (!primaryModels.includes(selectedModel)) selectedModel = primaryModels[0] || '';
  });

  $effect(() => {
    if (!selectedFallbackProvider) {
      selectedFallbackModel = '';
    } else if (!fallbackModels.includes(selectedFallbackModel)) {
      selectedFallbackModel = fallbackModels[0] || '';
    }
  });

  $effect(() => {
    if (form?.actionError) toast.error(form.actionError);
    if (form?.saved) toast.success('AI model gateway settings saved.');
  });

  /** @param {string} status */
  function statusClass(status) {
    if (status === 'completed') return 'bg-emerald-100 text-emerald-700';
    if (status === 'blocked') return 'bg-red-100 text-red-700';
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
    if (!value) return '-';
    return new Intl.DateTimeFormat(undefined, {
      dateStyle: 'medium',
      timeStyle: 'short'
    }).format(new Date(value));
  }

  /** @param {any} item */
  function credentialLabel(item) {
    if (!item?.configured) return 'Not configured';
    if (item.credential_source === 'tenant') {
      return item.key_hint ? `Tenant key ...${item.key_hint}` : 'Tenant key';
    }
    return 'Platform key';
  }

  /** @param {number | null} value */
  function formatCost(value) {
    if (value === null || value === undefined) return '-';
    return `$${(value / 1_000_000).toFixed(6)}`;
  }
</script>

<svelte:head>
  <title>AI Model Gateway - BottleCRM</title>
</svelte:head>

<PageHeader
  title="AI Safety Gateway"
  subtitle="Control, sanitize, route, and audit every external model request"
/>

<div class="space-y-6 p-6 md:p-8">
  {#if !primaryProviderStatus.configured}
    <div class="flex gap-3 rounded-lg border border-amber-200 bg-amber-50 p-4 text-amber-900">
      <AlertTriangle class="mt-0.5 size-5 shrink-0" />
      <div>
        <p class="text-sm font-medium">The primary provider has no API key</p>
        <p class="mt-1 text-xs">
          Configure a platform key or tenant key. Leads still use deterministic rule scoring if all
          model routes are unavailable.
        </p>
      </div>
    </div>
  {/if}

  <div class="grid gap-4 sm:grid-cols-4">
    <div
      class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-4"
    >
      <p class="text-xs text-[color:var(--text-muted)]">Gateway</p>
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
      <p class="text-xs text-[color:var(--text-muted)]">Primary route</p>
      <p class="mt-2 text-lg font-semibold text-[color:var(--text-primary)]">
        {catalog[configuration.provider]?.label || configuration.provider}
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

  <form method="POST" action="?/save" use:enhance class="space-y-6">
    <section
      class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-5"
    >
      <div class="mb-5 flex items-start gap-3">
        <Network class="mt-0.5 size-5 text-violet-500" />
        <div>
          <h2 class="font-medium text-[color:var(--text-primary)]">Provider routing</h2>
          <p class="text-xs text-[color:var(--text-muted)]">
            The fallback model runs only when the primary route fails or returns invalid output.
          </p>
        </div>
      </div>

      <div class="mb-5 grid gap-3 sm:grid-cols-3">
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
              >Apply research and scoring to new leads.</span
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
              >Read bounded public company pages.</span
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
              >Use provider routing and local validation.</span
            ></span
          >
        </label>
      </div>

      <div class="grid gap-4 lg:grid-cols-2">
        <div class="rounded-md border border-[color:var(--border-faint)] p-4">
          <h3 class="mb-3 text-sm font-medium">Primary model</h3>
          <div class="grid gap-3 sm:grid-cols-3">
            <div>
              <Label for="provider">Provider</Label>
              <select
                id="provider"
                name="provider"
                bind:value={selectedProvider}
                class="h-9 w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] px-3 text-sm"
              >
                {#each providerEntries as [provider, item] (provider)}
                  <option value={provider}>{item.label}</option>
                {/each}
              </select>
            </div>
            <div>
              <Label for="model">Model</Label>
              <select
                id="model"
                name="model"
                bind:value={selectedModel}
                class="h-9 w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] px-3 text-sm"
              >
                {#each primaryModels as model (model)}<option value={model}>{model}</option>{/each}
              </select>
            </div>
            <div>
              <Label for="reasoning-effort">Reasoning</Label>
              <select
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
          </div>
        </div>

        <div class="rounded-md border border-[color:var(--border-faint)] p-4">
          <h3 class="mb-3 text-sm font-medium">Fallback model</h3>
          <div class="grid gap-3 sm:grid-cols-3">
            <div>
              <Label for="fallback-provider">Provider</Label>
              <select
                id="fallback-provider"
                name="fallback_provider"
                bind:value={selectedFallbackProvider}
                class="h-9 w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] px-3 text-sm"
              >
                <option value="">Rules only</option>
                {#each providerEntries as [provider, item] (provider)}
                  <option value={provider}>{item.label}</option>
                {/each}
              </select>
            </div>
            <div>
              <Label for="fallback-model">Model</Label>
              <select
                id="fallback-model"
                name="fallback_model"
                bind:value={selectedFallbackModel}
                disabled={!selectedFallbackProvider}
                class="h-9 w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] px-3 text-sm disabled:opacity-50"
              >
                {#each fallbackModels as model (model)}<option value={model}>{model}</option>{/each}
              </select>
            </div>
            <div>
              <Label for="fallback-reasoning">Reasoning</Label>
              <select
                id="fallback-reasoning"
                name="fallback_reasoning_effort"
                value={configuration.fallback_reasoning_effort}
                disabled={!selectedFallbackProvider}
                class="h-9 w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] px-3 text-sm disabled:opacity-50"
              >
                {#each configuration.allowed_reasoning_efforts as effort (effort)}
                  <option value={effort}>{effort === 'none' ? 'None' : effort}</option>
                {/each}
              </select>
            </div>
          </div>
        </div>
      </div>
    </section>

    <section
      class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-5"
    >
      <div class="mb-5 flex items-start gap-3">
        <AlertTriangle class="mt-0.5 size-5 text-amber-500" />
        <div>
          <h2 class="font-medium text-[color:var(--text-primary)]">Data safety policy</h2>
          <p class="text-xs text-[color:var(--text-muted)]">
            Requests that violate these controls are blocked before any provider connection is
            opened. Raw prompts and responses are never written to the audit ledger.
          </p>
        </div>
      </div>

      <div class="grid gap-5 lg:grid-cols-2">
        <div class="space-y-4 rounded-md border border-[color:var(--border-faint)] p-4">
          <div>
            <h3 class="text-sm font-medium">Allowed providers</h3>
            <p class="text-xs text-[color:var(--text-muted)]">
              At least one provider is required; primary and fallback routes must be selected here.
            </p>
          </div>
          <div class="grid gap-2 sm:grid-cols-3">
            {#each providerEntries as [provider, item] (provider)}
              <label class="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  name="allowed_ai_providers"
                  value={provider}
                  checked={configuration.allowed_ai_providers?.includes(provider)}
                />
                {item.label}
              </label>
            {/each}
          </div>
        </div>

        <div class="space-y-4 rounded-md border border-[color:var(--border-faint)] p-4">
          <div>
            <h3 class="text-sm font-medium">Allowed purposes</h3>
            <p class="text-xs text-[color:var(--text-muted)]">
              Clearing every purpose blocks all model calls while preserving deterministic rules.
            </p>
          </div>
          <div class="grid gap-2 sm:grid-cols-2">
            <label class="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                name="allowed_ai_purposes"
                value="lead_qualification"
                checked={configuration.allowed_ai_purposes?.includes('lead_qualification')}
              />
              Lead qualification
            </label>
            <label class="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                name="allowed_ai_purposes"
                value="outbound_copy"
                checked={configuration.allowed_ai_purposes?.includes('outbound_copy')}
              />
              Outbound copy
            </label>
          </div>
        </div>
      </div>

      <div class="mt-5 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <div>
          <Label for="pii-handling">PII handling</Label>
          <select
            id="pii-handling"
            name="pii_handling"
            value={configuration.pii_handling}
            class="h-9 w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] px-3 text-sm"
          >
            <option value="redact">Redact before sending</option>
            <option value="block">Block the request</option>
            <option value="allow">Allow with tenant approval</option>
          </select>
        </div>
        <div>
          <Label for="max-ai-input-chars">Maximum input characters</Label>
          <Input
            id="max-ai-input-chars"
            name="max_ai_input_chars"
            type="number"
            min="1000"
            max="200000"
            value={configuration.max_ai_input_chars}
          />
        </div>
        <div>
          <Label for="max-ai-input-tokens">Maximum estimated tokens</Label>
          <Input
            id="max-ai-input-tokens"
            name="max_ai_input_tokens"
            type="number"
            min="256"
            max="100000"
            value={configuration.max_ai_input_tokens}
          />
        </div>
        <div>
          <Label for="ai-audit-retention-days">Audit retention (days)</Label>
          <Input
            id="ai-audit-retention-days"
            name="ai_audit_retention_days"
            type="number"
            min="1"
            max="3650"
            value={configuration.ai_audit_retention_days}
          />
        </div>
      </div>
      <p class="mt-3 text-xs text-[color:var(--text-muted)]">
        Credential-like secrets, private keys, payment cards, government IDs, and recovery keys are
        always blocked. This cannot be disabled by a tenant.
      </p>
    </section>

    <section
      class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-5"
    >
      <div class="mb-5 flex items-start gap-3">
        <KeyRound class="mt-0.5 size-5 text-blue-500" />
        <div>
          <h2 class="font-medium text-[color:var(--text-primary)]">Provider credentials</h2>
          <p class="text-xs text-[color:var(--text-muted)]">
            Tenant keys override platform keys, are encrypted at rest, and are never returned by the
            API.
          </p>
        </div>
      </div>
      <div class="grid gap-4 lg:grid-cols-3">
        {#each providerEntries as [provider, item] (provider)}
          <div class="rounded-md border border-[color:var(--border-faint)] p-4">
            <div class="mb-3 flex items-center justify-between gap-2">
              <div>
                <p class="text-sm font-medium">{item.label}</p>
                <p class="text-xs {item.configured ? 'text-emerald-600' : 'text-amber-600'}">
                  {credentialLabel(item)}
                </p>
              </div>
              <span
                class="rounded-full bg-[color:var(--bg-subtle)] px-2 py-0.5 text-[10px] text-[color:var(--text-muted)] uppercase"
                >{item.protocol}</span
              >
            </div>
            <Input
              name={`${provider}_api_key`}
              type="password"
              autocomplete="new-password"
              placeholder={configuration.tenant_keys_allowed
                ? 'Enter a new tenant API key'
                : 'Tenant keys disabled'}
              disabled={!configuration.tenant_keys_allowed}
            />
            {#if item.credential_source === 'tenant'}
              <label class="mt-2 flex items-center gap-2 text-xs text-[color:var(--text-muted)]">
                <input type="checkbox" name={`clear_${provider}_api_key`} />Remove tenant key
              </label>
            {/if}
          </div>
        {/each}
      </div>
    </section>

    <section
      class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-5"
    >
      <div class="mb-5 flex items-start gap-3">
        <WandSparkles class="mt-0.5 size-5 text-violet-500" />
        <div>
          <h2 class="font-medium text-[color:var(--text-primary)]">Qualification policy</h2>
          <p class="text-xs text-[color:var(--text-muted)]">
            These instructions apply only to the current organization.
          </p>
        </div>
      </div>
      <div class="mb-4 grid gap-4 sm:grid-cols-4">
        <div class="sm:col-span-2">
          <Label for="icp-description">Ideal customer profile</Label>
          <textarea
            id="icp-description"
            name="icp_description"
            rows="5"
            maxlength="5000"
            class="mt-1 w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-3 text-sm"
            placeholder="Industries, company sizes, countries, buyer roles, and use cases"
            >{configuration.icp_description}</textarea
          >
        </div>
        <div>
          <Label for="max-pages">Maximum research pages</Label>
          <Input
            id="max-pages"
            name="max_research_pages"
            type="number"
            min="1"
            max="3"
            value={configuration.max_research_pages}
          />
        </div>
        <div>
          <Label for="web-timeout">Website timeout</Label>
          <Input
            id="web-timeout"
            name="website_timeout_seconds"
            type="number"
            min="1"
            max="15"
            value={configuration.website_timeout_seconds}
          />
        </div>
      </div>
      <div class="grid gap-4 sm:grid-cols-2">
        <div>
          <Label for="positive-signals">Positive signals</Label>
          <textarea
            id="positive-signals"
            name="positive_signals"
            rows="4"
            maxlength="5000"
            class="mt-1 w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-3 text-sm"
            placeholder="Multi-location operation, senior buyer, urgent automation project"
            >{configuration.positive_signals}</textarea
          >
        </div>
        <div>
          <Label for="negative-signals">Negative or disqualifying signals</Label>
          <textarea
            id="negative-signals"
            name="negative_signals"
            rows="4"
            maxlength="5000"
            class="mt-1 w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-3 text-sm"
            placeholder="Student, competitor, unsupported geography, personal project"
            >{configuration.negative_signals}</textarea
          >
        </div>
      </div>
      <div class="mt-5 flex justify-end">
        <Button type="submit" class="gap-1.5"
          ><Sparkles class="size-4" />Save gateway settings</Button
        >
      </div>
    </section>
  </form>

  <section
    class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)]"
  >
    <div class="flex items-start gap-3 border-b border-[color:var(--border-faint)] p-5">
      <Network class="mt-0.5 size-5 text-violet-500" />
      <div>
        <h2 class="font-medium text-[color:var(--text-primary)]">AI call audit</h2>
        <p class="text-xs text-[color:var(--text-muted)]">
          Tenant, purpose, prompt/config version, routing, cost, latency, and safe failure metadata.
        </p>
      </div>
    </div>
    {#if audits.length}
      <div class="overflow-x-auto">
        <table class="w-full min-w-[1200px] text-left text-sm">
          <thead class="bg-[color:var(--bg-subtle)] text-xs text-[color:var(--text-muted)]">
            <tr
              ><th class="px-4 py-3 font-medium">Time / purpose</th><th
                class="px-4 py-3 font-medium">Status</th
              ><th class="px-4 py-3 font-medium">Route</th><th class="px-4 py-3 font-medium"
                >Version / input</th
              ><th class="px-4 py-3 font-medium">PII policy</th><th class="px-4 py-3 font-medium"
                >Usage</th
              ><th class="px-4 py-3 font-medium">Failure</th></tr
            >
          </thead>
          <tbody class="divide-y divide-[color:var(--border-faint)]">
            {#each audits as item (item.id)}
              <tr class="align-top">
                <td class="px-4 py-3">
                  <p class="font-medium text-[color:var(--text-primary)]">{item.purpose}</p>
                  <p class="mt-1 text-xs text-[color:var(--text-muted)]">
                    {formatDate(item.created_at)}
                  </p>
                </td>
                <td class="px-4 py-3">
                  <span class="rounded-full px-2 py-0.5 text-[11px] {statusClass(item.status)}">
                    {item.status}
                  </span>
                  {#if item.fallback_used}<p class="mt-1 text-xs text-amber-600">
                      Fallback used
                    </p>{/if}
                </td>
                <td class="px-4 py-3">
                  <p class="font-medium">{item.provider || '-'}</p>
                  <p class="text-xs text-[color:var(--text-muted)]">{item.model || '-'}</p>
                  <p class="text-[10px] text-[color:var(--text-muted)]">
                    route {item.route_index} · {item.credential_source || 'no credential'}
                  </p>
                </td>
                <td class="px-4 py-3">
                  <p class="text-xs">{item.prompt_version}</p>
                  <p class="font-mono text-[10px] text-[color:var(--text-muted)]">
                    cfg {item.configuration_sha256?.slice(0, 10)} · input {item.input_sha256?.slice(
                      0,
                      10
                    )}
                  </p>
                  <p class="text-[10px] text-[color:var(--text-muted)]">
                    {item.field_paths?.length || 0} approved field(s)
                  </p>
                </td>
                <td class="px-4 py-3">
                  <p class="text-xs">{item.redaction_count || 0} redaction(s)</p>
                  <p class="text-[10px] text-[color:var(--text-muted)]">
                    {Object.entries(item.pii_findings || {})
                      .map(([kind, count]) => `${kind}:${count}`)
                      .join(' · ') || 'No PII detected'}
                  </p>
                </td>
                <td class="px-4 py-3">
                  <p class="text-xs">
                    {item.input_tokens ?? item.estimated_input_tokens} in / {item.output_tokens ??
                      '-'} out
                  </p>
                  <p class="text-[10px] text-[color:var(--text-muted)]">
                    {item.latency_ms ?? '-'} ms · {formatCost(item.estimated_cost_microusd)}
                  </p>
                </td>
                <td class="max-w-[260px] px-4 py-3">
                  <p class="text-xs text-red-600">{item.failure_code || '-'}</p>
                  {#if item.failure_reason}<p
                      class="mt-1 line-clamp-2 text-[10px] text-[color:var(--text-muted)]"
                    >
                      {item.failure_reason}
                    </p>{/if}
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {:else}
      <div class="p-8 text-center">
        <CheckCircle2 class="mx-auto size-8 text-emerald-500" />
        <p class="mt-3 text-sm font-medium">No external model calls yet</p>
        <p class="mt-1 text-xs text-[color:var(--text-muted)]">
          Blocked, failed, and completed attempts will appear here without raw prompt content.
        </p>
      </div>
    {/if}
  </section>

  <section
    class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)]"
  >
    <div class="flex items-start gap-3 border-b border-[color:var(--border-faint)] p-5">
      <Bot class="mt-0.5 size-5 text-blue-500" />
      <div>
        <h2 class="font-medium text-[color:var(--text-primary)]">Inspection ledger</h2>
        <p class="text-xs text-[color:var(--text-muted)]">
          Provider attempts, model usage, evidence, and fallback decisions.
        </p>
      </div>
    </div>
    {#if inspections.length}
      <div class="overflow-x-auto">
        <table class="w-full min-w-[1100px] text-left text-sm">
          <thead class="bg-[color:var(--bg-subtle)] text-xs text-[color:var(--text-muted)]">
            <tr
              ><th class="px-4 py-3 font-medium">Lead</th><th class="px-4 py-3 font-medium"
                >Status</th
              ><th class="px-4 py-3 font-medium">Score</th><th class="px-4 py-3 font-medium"
                >Route</th
              ><th class="px-4 py-3 font-medium">Evidence</th><th class="px-4 py-3 font-medium"
                >Completed</th
              ></tr
            >
          </thead>
          <tbody class="divide-y divide-[color:var(--border-faint)]">
            {#each inspections as item (item.id)}
              <tr class="align-top">
                <td class="px-4 py-3"
                  ><p class="font-medium text-[color:var(--text-primary)]">
                    {item.company_name || 'Unknown company'}
                  </p>
                  <p class="mt-1 text-xs text-[color:var(--text-muted)]">
                    {item.source} - {item.source_record_id}
                  </p></td
                >
                <td class="px-4 py-3"
                  ><span class="rounded-full px-2 py-0.5 text-[11px] {statusClass(item.status)}"
                    >{item.status}</span
                  >{#if item.fallback_kind}<p class="mt-1 text-xs text-amber-600">
                      Fallback: {item.fallback_kind}
                    </p>{/if}</td
                >
                <td class="px-4 py-3"
                  ><p class="text-lg font-semibold {bandClass(item.qualification_band)}">
                    {item.qualification_score ?? '-'}
                  </p>
                  <p class="text-xs text-[color:var(--text-muted)] capitalize">
                    {item.qualification_band}
                  </p></td
                >
                <td class="px-4 py-3"
                  ><p class="font-medium">{item.provider || '-'}</p>
                  <p class="mt-1 text-xs text-[color:var(--text-muted)]">{item.model || '-'}</p>
                  <p class="text-xs text-[color:var(--text-muted)]">
                    {item.input_tokens ?? 0} in / {item.output_tokens ?? 0} out
                  </p>
                  {#if item.provider_attempts?.length}<p
                      class="mt-1 text-[10px] text-[color:var(--text-muted)]"
                    >
                      {item.provider_attempts
                        .map((attempt) => `${attempt.provider}:${attempt.status}`)
                        .join(' -> ')}
                    </p>{/if}</td
                >
                <td class="max-w-[360px] px-4 py-3"
                  ><p class="line-clamp-2 text-xs text-[color:var(--text-muted)]">
                    {item.research_summary ||
                      item.qualification_reasons?.join(' - ') ||
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
          Enable the gateway; new inbound leads will appear here.
        </p>
      </div>
    {/if}
  </section>
</div>
