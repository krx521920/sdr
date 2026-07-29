<script>
  import { enhance } from '$app/forms';
  import { Button } from '$lib/components/ui/button/index.js';
  import { Input } from '$lib/components/ui/input/index.js';
  import { Label } from '$lib/components/ui/label/index.js';
  import { PageHeader } from '$lib/components/layout';
  import { toast } from 'svelte-sonner';
  import { ArrowRight, FlaskConical, Pencil, Plus, Route, Trash2, Users } from '@lucide/svelte';

  /** @type {{ data: any, form: any }} */
  let { data, form } = $props();

  let editorOpen = $state(false);
  let editingId = $state('');
  let name = $state('');
  let priority = $state(100);
  let strategy = $state('least_loaded');
  let isActive = $state(true);
  let countries = $state('');
  let selectedSources = $state([]);
  let selectedBands = $state([]);
  let selectedProfiles = $state([]);

  const activeRules = $derived(data.rules.filter((rule) => rule.is_active).length);

  $effect(() => {
    if (form?.actionError) toast.error(form.actionError);
    if (form?.saved) {
      toast.success('SDR routing rule saved.');
      editorOpen = false;
    }
    if (form?.deleted) toast.success('SDR routing rule deleted.');
  });

  function openCreate() {
    editingId = '';
    name = '';
    priority = 100;
    strategy = 'least_loaded';
    isActive = true;
    countries = '';
    selectedSources = [];
    selectedBands = [];
    selectedProfiles = [];
    editorOpen = true;
  }

  /** @param {any} rule */
  function openEdit(rule) {
    editingId = rule.id;
    name = rule.name;
    priority = rule.priority;
    strategy = rule.strategy;
    isActive = rule.is_active;
    countries = (rule.countries || []).join(', ');
    selectedSources = [...(rule.sources || [])];
    selectedBands = [...(rule.qualification_bands || [])];
    selectedProfiles = (rule.members || []).map((member) => member.profile_id);
    editorOpen = true;
  }

  /** @param {string[]} values @param {string} value */
  function toggled(values, value) {
    return values.includes(value) ? values.filter((item) => item !== value) : [...values, value];
  }

  /** @param {string} strategyValue */
  function strategyLabel(strategyValue) {
    return data.strategies.find((item) => item.value === strategyValue)?.label || strategyValue;
  }

  /** @param {string[]} values @param {any[]} choices */
  function labels(values, choices) {
    if (!values?.length) return 'Any';
    return values
      .map(
        (value) => choices.find((choice) => (choice.value || choice.code) === value)?.label || value
      )
      .join(', ');
  }

  /** @param {string | null} profileId */
  function profileName(profileId) {
    return (
      data.profiles.find((profile) => profile.id === profileId)?.name || profileId || 'No assignee'
    );
  }
</script>

<svelte:head>
  <title>SDR Routing - BottleCRM</title>
</svelte:head>

<PageHeader
  title="SDR Routing"
  subtitle="Route inbound leads by country, source, qualification, and sales capacity"
/>

<div class="space-y-6 p-6 md:p-8">
  <div class="grid gap-4 sm:grid-cols-3">
    <div
      class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-4"
    >
      <p class="text-xs text-[color:var(--text-muted)]">Configured rules</p>
      <p class="mt-2 text-2xl font-semibold text-[color:var(--text-primary)]">
        {data.rules.length}
      </p>
    </div>
    <div
      class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-4"
    >
      <p class="text-xs text-[color:var(--text-muted)]">Active rules</p>
      <p class="mt-2 text-2xl font-semibold text-emerald-600">{activeRules}</p>
    </div>
    <div
      class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-4"
    >
      <p class="text-xs text-[color:var(--text-muted)]">Eligible sales users</p>
      <p class="mt-2 text-2xl font-semibold text-[color:var(--text-primary)]">
        {data.profiles.length}
      </p>
    </div>
  </div>

  <section
    class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)]"
  >
    <div
      class="flex flex-wrap items-center justify-between gap-3 border-b border-[color:var(--border-faint)] p-4"
    >
      <div>
        <h2 class="font-medium text-[color:var(--text-primary)]">Assignment rules</h2>
        <p class="text-xs text-[color:var(--text-muted)]">
          Lower priority numbers run first. The first usable match wins.
        </p>
      </div>
      <Button onclick={openCreate} class="gap-1.5"><Plus class="size-4" />New rule</Button>
    </div>

    {#if !data.profiles.length}
      <div class="m-4 rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-800">
        No active user has Sales access. Enable Sales access under Users & Teams before creating a
        rule.
      </div>
    {/if}

    {#if data.rules.length}
      <ul class="divide-y divide-[color:var(--border-faint)]">
        {#each data.rules as rule (rule.id)}
          <li class="flex flex-col gap-4 p-4 lg:flex-row lg:items-start lg:justify-between">
            <div class="min-w-0 space-y-2">
              <div class="flex flex-wrap items-center gap-2">
                <span class="rounded bg-[color:var(--bg-subtle)] px-2 py-0.5 text-xs font-medium"
                  >#{rule.priority}</span
                >
                <h3 class="font-medium text-[color:var(--text-primary)]">{rule.name}</h3>
                <span
                  class="rounded-full px-2 py-0.5 text-[11px] {rule.is_active
                    ? 'bg-emerald-100 text-emerald-700'
                    : 'bg-slate-100 text-slate-500'}"
                >
                  {rule.is_active ? 'Active' : 'Disabled'}
                </span>
                <span class="rounded-full bg-blue-50 px-2 py-0.5 text-[11px] text-blue-700"
                  >{strategyLabel(rule.strategy)}</span
                >
              </div>
              <div
                class="grid gap-x-8 gap-y-1 text-xs text-[color:var(--text-muted)] sm:grid-cols-3"
              >
                <p>
                  <span class="font-medium">Countries:</span>
                  {rule.countries?.length ? rule.countries.join(', ') : 'Any'}
                </p>
                <p>
                  <span class="font-medium">Sources:</span>
                  {labels(rule.sources, data.sources)}
                </p>
                <p>
                  <span class="font-medium">Scores:</span>
                  {labels(rule.qualification_bands, data.qualification_bands)}
                </p>
              </div>
              <div class="flex flex-wrap items-center gap-2 text-xs text-[color:var(--text-muted)]">
                <Users class="size-3.5" />
                {#each rule.members as member, index (member.profile_id)}
                  <span
                    >{member.name || member.email}{index < rule.members.length - 1 ? ',' : ''}</span
                  >
                {/each}
              </div>
            </div>
            <div class="flex shrink-0 gap-2">
              <Button size="sm" variant="outline" onclick={() => openEdit(rule)} class="gap-1"
                ><Pencil class="size-3.5" />Edit</Button
              >
              <form method="POST" action="?/delete" use:enhance>
                <input type="hidden" name="id" value={rule.id} />
                <Button
                  type="submit"
                  size="icon"
                  variant="ghost"
                  class="text-red-600"
                  aria-label="Delete rule"><Trash2 class="size-4" /></Button
                >
              </form>
            </div>
          </li>
        {/each}
      </ul>
    {:else}
      <div class="p-10 text-center">
        <Route class="mx-auto size-8 text-[color:var(--text-muted)]" />
        <p class="mt-3 text-sm font-medium text-[color:var(--text-primary)]">
          No SDR routing rules yet
        </p>
        <p class="mt-1 text-xs text-[color:var(--text-muted)]">
          Leads currently use the global least-loaded fallback.
        </p>
      </div>
    {/if}
  </section>

  {#if editorOpen}
    <section class="rounded-lg border border-blue-200 bg-[color:var(--bg-elevated)] p-5">
      <div class="mb-5 flex items-center justify-between">
        <div>
          <h2 class="font-medium text-[color:var(--text-primary)]">
            {editingId ? 'Edit rule' : 'New routing rule'}
          </h2>
          <p class="text-xs text-[color:var(--text-muted)]">
            Empty condition groups match every value.
          </p>
        </div>
        <Button variant="ghost" onclick={() => (editorOpen = false)}>Cancel</Button>
      </div>
      <form
        method="POST"
        action={editingId ? '?/update' : '?/create'}
        use:enhance
        class="space-y-5"
      >
        {#if editingId}<input type="hidden" name="id" value={editingId} />{/if}
        <input type="hidden" name="sources" value={JSON.stringify(selectedSources)} />
        <input type="hidden" name="qualification_bands" value={JSON.stringify(selectedBands)} />
        <input type="hidden" name="profile_ids" value={JSON.stringify(selectedProfiles)} />

        <div class="grid gap-4 sm:grid-cols-3">
          <div class="sm:col-span-2">
            <Label for="rule-name">Rule name</Label><Input
              id="rule-name"
              name="name"
              bind:value={name}
              required
            />
          </div>
          <div>
            <Label for="rule-priority">Priority</Label><Input
              id="rule-priority"
              name="priority"
              type="number"
              min="0"
              bind:value={priority}
            />
          </div>
          <div>
            <Label for="rule-strategy">Strategy</Label><select
              id="rule-strategy"
              name="strategy"
              bind:value={strategy}
              class="h-9 w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] px-3 text-sm"
              >{#each data.strategies as item (item.value)}<option value={item.value}
                  >{item.label}</option
                >{/each}</select
            >
          </div>
          <div class="sm:col-span-2">
            <Label for="rule-countries">Countries</Label><Input
              id="rule-countries"
              name="countries"
              bind:value={countries}
              placeholder="US, GB, CN (empty means any country)"
            />
            <p class="mt-1 text-xs text-[color:var(--text-muted)]">
              Use ISO country codes separated by commas.
            </p>
          </div>
        </div>

        <div class="grid gap-5 lg:grid-cols-3">
          <fieldset>
            <legend class="mb-2 text-sm font-medium">Lead sources</legend>
            <div class="space-y-1.5">
              {#each data.sources as source (source.value)}<label
                  class="flex items-center gap-2 text-sm"
                  ><input
                    type="checkbox"
                    checked={selectedSources.includes(source.value)}
                    onchange={() => (selectedSources = toggled(selectedSources, source.value))}
                  />{source.label}</label
                >{/each}
            </div>
          </fieldset>
          <fieldset>
            <legend class="mb-2 text-sm font-medium">Qualification</legend>
            <div class="space-y-1.5">
              {#each data.qualification_bands as band (band.value)}<label
                  class="flex items-center gap-2 text-sm"
                  ><input
                    type="checkbox"
                    checked={selectedBands.includes(band.value)}
                    onchange={() => (selectedBands = toggled(selectedBands, band.value))}
                  />{band.label}</label
                >{/each}
            </div>
          </fieldset>
          <fieldset>
            <legend class="mb-2 text-sm font-medium">Sales pool</legend>
            <div class="space-y-1.5">
              {#each data.profiles as profile (profile.id)}<label
                  class="flex items-center gap-2 text-sm"
                  ><input
                    type="checkbox"
                    checked={selectedProfiles.includes(profile.id)}
                    onchange={() => (selectedProfiles = toggled(selectedProfiles, profile.id))}
                  />{profile.name}</label
                >{/each}
            </div>
          </fieldset>
        </div>
        <label class="flex items-center gap-2 text-sm"
          ><input type="checkbox" name="is_active" bind:checked={isActive} />Active</label
        >
        <div class="flex justify-end">
          <Button type="submit" disabled={!selectedProfiles.length}
            >{editingId ? 'Save changes' : 'Create rule'}<ArrowRight
              class="ml-1.5 size-4"
            /></Button
          >
        </div>
      </form>
    </section>
  {/if}

  <section
    class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-5"
  >
    <div class="mb-4 flex items-center gap-2">
      <FlaskConical class="size-5 text-violet-500" />
      <div>
        <h2 class="font-medium text-[color:var(--text-primary)]">Routing preview</h2>
        <p class="text-xs text-[color:var(--text-muted)]">
          Evaluate live rules without creating a lead or advancing round-robin.
        </p>
      </div>
    </div>
    <form method="POST" action="?/preview" use:enhance class="grid items-end gap-3 sm:grid-cols-4">
      <div>
        <Label for="preview-country">Country</Label><Input
          id="preview-country"
          name="country"
          placeholder="US"
        />
      </div>
      <div>
        <Label for="preview-source">Source</Label><select
          id="preview-source"
          name="source"
          class="h-9 w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] px-3 text-sm"
          >{#each data.sources as source (source.value)}<option value={source.value}
              >{source.label}</option
            >{/each}</select
        >
      </div>
      <div>
        <Label for="preview-band">Qualification</Label><select
          id="preview-band"
          name="qualification_band"
          class="h-9 w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] px-3 text-sm"
          >{#each data.qualification_bands as band (band.value)}<option value={band.value}
              >{band.label}</option
            >{/each}</select
        >
      </div>
      <Button type="submit" variant="outline" class="gap-1.5"
        ><FlaskConical class="size-4" />Preview</Button
      >
    </form>
    {#if form?.preview}
      <div class="mt-4 rounded-md bg-[color:var(--bg-subtle)] p-3 text-sm">
        <p class="font-medium text-[color:var(--text-primary)]">
          {form.preview.matched
            ? `Assigned to ${profileName(form.preview.assigned_profile_id)}`
            : `Fallback assigns ${profileName(form.preview.assigned_profile_id)}`}
        </p>
        <p class="mt-1 text-xs text-[color:var(--text-muted)]">{form.preview.reason}</p>
      </div>
    {/if}
  </section>
</div>
