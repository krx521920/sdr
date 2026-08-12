<script>
  import { enhance } from '$app/forms';
  import { PageHeader } from '$lib/components/layout';
  import { LinkedinIcon as Linkedin } from '$lib/components/icons';
  import { Button } from '$lib/components/ui/button/index.js';
  import {
    AlertTriangle,
    ArrowUpRight,
    Ban,
    Building2,
    CheckCircle2,
    CircleDot,
    FileSpreadsheet,
    LoaderCircle,
    Mail,
    Pause,
    Phone,
    Plus,
    RefreshCw,
    Rocket,
    Target,
    Upload,
    Users
  } from '@lucide/svelte';
  import { toast } from 'svelte-sonner';

  /** @type {{ data: any, form: any }} */
  let { data, form } = $props();

  const campaigns = $derived(data.campaigns?.results || []);
  const summary = $derived(data.campaigns?.summary || {});
  const selected = $derived(data.selectedCampaign || null);
  const prospects = $derived(data.prospects?.results || []);
  const prospectSummary = $derived(data.prospects?.summary || {});
  const outboundSequences = $derived(data.campaigns?.outbound_sequences || []);

  let creating = $state(false);
  let name = $state('');
  let description = $state('');
  let icpDescription = $state('');
  let status = $state('draft');
  let selectedChannels = $state([]);
  let sequenceId = $state('');
  let dailySendLimit = $state(50);
  let csvText = $state('');

  $effect(() => {
    const campaign = selected;
    if (!creating && campaign) {
      name = campaign.name || '';
      description = campaign.description || '';
      icpDescription = campaign.icp_description || '';
      status = campaign.status || 'draft';
      selectedChannels = [...(campaign.channels || [])];
      sequenceId = campaign.sequence_id || '';
      dailySendLimit = campaign.daily_send_limit || 50;
    }
  });

  $effect(() => {
    if (form?.actionError) toast.error(form.actionError);
    if (form?.campaignSaved) {
      toast.success('Outbound campaign saved.');
      creating = false;
    }
    if (form?.prospectsImported) {
      toast.success(
        `${form.importResult?.created || 0} prospects imported${form.importResult?.queued ? `, ${form.importResult.queued} queued` : ''}.`
      );
      csvText = '';
    }
    if (form?.prospectUpdated) toast.success('Prospect status updated.');
    if (form?.campaignUpdated) {
      const queued = form.campaignExecution?.queued || 0;
      toast.success(
        form.campaignAction === 'launch'
          ? `Campaign launched${queued ? `, ${queued} prospects queued` : ''}.`
          : `Campaign ${form.campaignAction.replace('_', ' ')} completed.`
      );
    }
  });

  function startCreate() {
    creating = true;
    name = '';
    description = '';
    icpDescription = '';
    status = 'draft';
    selectedChannels = ['email', 'linkedin'];
    sequenceId = '';
    dailySendLimit = 50;
  }

  function cancelCreate() {
    creating = false;
  }

  /** @param {string} channel */
  function toggleChannel(channel) {
    selectedChannels = selectedChannels.includes(channel)
      ? selectedChannels.filter((value) => value !== channel)
      : [...selectedChannels, channel];
  }

  /** @param {string} value */
  function statusClass(value) {
    if (value === 'active' || value === 'promoted') return 'bg-emerald-100 text-emerald-700';
    if (value === 'queued' || value === 'processing') return 'bg-blue-100 text-blue-700';
    if (value === 'failed') return 'bg-red-100 text-red-700';
    if (value === 'paused' || value === 'disqualified') return 'bg-amber-100 text-amber-700';
    return 'bg-slate-100 text-slate-600';
  }

  /** @param {string | null} value */
  function formatDate(value) {
    if (!value) return '-';
    return new Intl.DateTimeFormat(undefined, {
      dateStyle: 'medium',
      timeStyle: 'short'
    }).format(new Date(value));
  }

  /** @param {any} prospect */
  function contactName(prospect) {
    return [prospect.first_name, prospect.last_name].filter(Boolean).join(' ') || prospect.email || 'Company prospect';
  }

  /** @param {string} channel */
  function channelIcon(channel) {
    if (channel === 'email') return Mail;
    if (channel === 'linkedin') return Linkedin;
    if (channel === 'phone' || channel === 'whatsapp') return Phone;
    return CircleDot;
  }
</script>

<svelte:head>
  <title>SDR Outbound - BottleCRM</title>
</svelte:head>

<PageHeader
  title="SDR Outbound"
  subtitle="Build clean prospect lists, prevent duplicate outreach, and promote qualified accounts into the SDR pipeline"
>
  {#snippet titleIcon()}
    <Target class="size-4" />
  {/snippet}
  {#snippet actions()}
    <Button size="sm" onclick={startCreate}>
      <Plus class="size-3.5" />
      New campaign
    </Button>
  {/snippet}
</PageHeader>

<div class="space-y-6 p-6 md:p-8">
  {#if data.loadError}
    <div class="flex gap-3 rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-800">
      <AlertTriangle class="mt-0.5 size-4 shrink-0" />
      <div>
        <p class="font-semibold">Outbound workspace is temporarily unavailable</p>
        <p class="mt-1 text-xs">{data.loadError}</p>
      </div>
    </div>
  {/if}

  <section class="grid gap-4 sm:grid-cols-2 xl:grid-cols-5">
    {#each [
      { label: 'Campaigns', value: summary.campaigns || 0, icon: Target, color: 'text-violet-600' },
      { label: 'Active', value: summary.active_campaigns || 0, icon: Rocket, color: 'text-blue-600' },
      { label: 'Prospects', value: summary.prospects || 0, icon: Users, color: 'text-slate-600' },
      { label: 'Ready', value: summary.ready || 0, icon: CircleDot, color: 'text-amber-600' },
      { label: 'Promoted', value: summary.promoted || 0, icon: CheckCircle2, color: 'text-emerald-600' }
    ] as card (card.label)}
      <div class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-4">
        <div class="flex items-center justify-between">
          <p class="text-xs text-[color:var(--text-muted)]">{card.label}</p>
          <card.icon class={`size-4 ${card.color}`} />
        </div>
        <p class="mt-2 text-2xl font-semibold tabular-nums">{card.value}</p>
      </div>
    {/each}
  </section>

  <div class="grid gap-6 xl:grid-cols-[320px_minmax(0,1fr)]">
    <section class="overflow-hidden rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)]">
      <div class="border-b border-[color:var(--border-faint)] p-4">
        <h2 class="text-sm font-semibold">Campaigns</h2>
        <p class="mt-1 text-xs text-[color:var(--text-muted)]">One ICP and channel mix per list</p>
      </div>
      <div class="max-h-[620px] divide-y divide-[color:var(--border-faint)] overflow-y-auto">
        {#each campaigns as campaign (campaign.id)}
          <a
            href={`?campaign=${campaign.id}`}
            class="block p-4 transition-colors hover:bg-[color:var(--bg-muted)] {selected?.id === campaign.id && !creating ? 'bg-[color:var(--bg-muted)]' : ''}"
          >
            <div class="flex items-start justify-between gap-3">
              <div class="min-w-0">
                <p class="truncate text-sm font-medium text-[color:var(--text)]">{campaign.name}</p>
                <p class="mt-1 truncate text-[11px] text-[color:var(--text-muted)]">
                  {campaign.metrics?.total || 0} prospects · {campaign.metrics?.promoted || 0} promoted
                </p>
                {#if campaign.sequence_name}
                  <p class="mt-1 truncate text-[10px] text-violet-600">{campaign.sequence_name}</p>
                {/if}
              </div>
              <span class={`rounded-full px-2 py-0.5 text-[10px] font-medium ${statusClass(campaign.status)}`}>
                {campaign.status}
              </span>
            </div>
            {#if campaign.channels?.length}
              <div class="mt-3 flex items-center gap-1.5">
                {#each campaign.channels as channel (channel)}
                  {@const Icon = channelIcon(channel)}
                  <span class="flex size-6 items-center justify-center rounded bg-[color:var(--bg-elevated)] text-[color:var(--text-muted)]" title={channel}>
                    <Icon class="size-3" />
                  </span>
                {/each}
              </div>
            {/if}
          </a>
        {:else}
          <div class="p-8 text-center text-xs text-[color:var(--text-muted)]">
            Create the first outbound campaign to start a prospect list.
          </div>
        {/each}
      </div>
    </section>

    <section class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-5">
      {#if creating || selected}
        <div class="flex items-start justify-between gap-4">
          <div>
            <h2 class="text-sm font-semibold">{creating ? 'New outbound campaign' : 'Campaign brief'}</h2>
            <p class="mt-1 text-xs text-[color:var(--text-muted)]">
              Define the ICP and allowed contact channels before importing a list.
            </p>
          </div>
          {#if creating}
            <Button size="sm" variant="ghost" onclick={cancelCreate}>Cancel</Button>
          {/if}
        </div>

        <form method="POST" action="?/saveCampaign" use:enhance class="mt-5 space-y-4">
          <input type="hidden" name="campaign_id" value={creating ? '' : selected?.id || ''} />
          <div class="grid gap-4 md:grid-cols-[minmax(0,1fr)_180px]">
            <label class="space-y-1.5 text-xs font-medium">
              Campaign name
              <input
                name="name"
                bind:value={name}
                required
                maxlength="160"
                class="h-9 w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg)] px-3 text-sm font-normal"
                placeholder="Industrial robotics — DACH"
              />
            </label>
            <label class="space-y-1.5 text-xs font-medium">
              Status
              <select
                bind:value={status}
                disabled
                class="h-9 w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg)] px-3 text-sm font-normal"
              >
                {#each data.campaigns?.statuses || [] as option (option.value)}
                  <option value={option.value}>{option.label}</option>
                {/each}
              </select>
              <input type="hidden" name="status" value={status} />
            </label>
          </div>
          <div class="grid gap-4 md:grid-cols-[minmax(0,1fr)_180px]">
            <label class="space-y-1.5 text-xs font-medium">
              Outbound email sequence
              <select
                name="sequence_id"
                bind:value={sequenceId}
                class="h-9 w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg)] px-3 text-sm font-normal"
              >
                <option value="">Select before launch</option>
                {#each outboundSequences as sequence (sequence.id)}
                  <option value={sequence.id}>
                    {sequence.name}{sequence.ready ? '' : ' (not ready)'}
                  </option>
                {/each}
              </select>
            </label>
            <label class="space-y-1.5 text-xs font-medium">
              Daily release limit
              <input
                name="daily_send_limit"
                type="number"
                min="1"
                max="1000"
                bind:value={dailySendLimit}
                class="h-9 w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg)] px-3 text-sm font-normal"
              />
            </label>
          </div>
          {#if !outboundSequences.length}
            <p class="rounded-md bg-amber-50 px-3 py-2 text-xs text-amber-800">
              Create and enable a nurture sequence that explicitly includes the outbound source before launch.
              <a href="/settings/sdr-nurturing" class="font-medium underline">Configure sequences</a>
            </p>
          {/if}
          <label class="block space-y-1.5 text-xs font-medium">
            ICP definition
            <textarea
              name="icp_description"
              bind:value={icpDescription}
              rows="3"
              class="w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg)] px-3 py-2 text-sm font-normal"
              placeholder="Industry × company size × role × pain point × geography"
            ></textarea>
          </label>
          <label class="block space-y-1.5 text-xs font-medium">
            Notes
            <textarea
              name="description"
              bind:value={description}
              rows="2"
              class="w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg)] px-3 py-2 text-sm font-normal"
              placeholder="Value proposition, exclusions, and list-building notes"
            ></textarea>
          </label>
          <fieldset>
            <legend class="text-xs font-medium">Contact channels</legend>
            <div class="mt-2 flex flex-wrap gap-2">
              {#each data.campaigns?.channels || [] as channel (channel)}
                {@const Icon = channelIcon(channel)}
                <label class="cursor-pointer">
                  <input
                    type="checkbox"
                    name="channels"
                    value={channel}
                    checked={selectedChannels.includes(channel)}
                    onchange={() => toggleChannel(channel)}
                    class="peer sr-only"
                  />
                  <span class="flex items-center gap-1.5 rounded-md border border-[color:var(--border-faint)] px-3 py-2 text-xs capitalize text-[color:var(--text-muted)] peer-checked:border-violet-300 peer-checked:bg-violet-50 peer-checked:text-violet-700">
                    <Icon class="size-3.5" />
                    {channel}
                  </span>
                </label>
              {/each}
            </div>
          </fieldset>
          <div class="flex justify-end">
            <Button size="sm" type="submit">{creating ? 'Create campaign' : 'Save campaign'}</Button>
          </div>
        </form>
        {#if !creating && selected}
          <div class="mt-4 flex flex-wrap items-center justify-between gap-3 border-t border-[color:var(--border-faint)] pt-4">
            <div class="text-[11px] text-[color:var(--text-muted)]">
              Run {selected.run_count || 0} · limit {selected.daily_send_limit || 50}/day
              {#if selected.last_refilled_at} · last refill {formatDate(selected.last_refilled_at)}{/if}
            </div>
            <div class="flex flex-wrap gap-2">
              {#if selected.status === 'active'}
                {#if (selected.metrics?.failed || 0) > 0}
                  <form method="POST" action="?/campaignAction" use:enhance>
                    <input type="hidden" name="campaign_id" value={selected.id} />
                    <input type="hidden" name="campaign_action" value="retry_failed" />
                    <Button size="sm" variant="outline" type="submit"><RefreshCw class="size-3.5" />Retry failed</Button>
                  </form>
                {/if}
                <form method="POST" action="?/campaignAction" use:enhance>
                  <input type="hidden" name="campaign_id" value={selected.id} />
                  <input type="hidden" name="campaign_action" value="pause" />
                  <Button size="sm" variant="outline" type="submit"><Pause class="size-3.5" />Pause</Button>
                </form>
              {:else if selected.status !== 'archived'}
                <form method="POST" action="?/campaignAction" use:enhance>
                  <input type="hidden" name="campaign_id" value={selected.id} />
                  <input type="hidden" name="campaign_action" value="launch" />
                  <Button size="sm" type="submit"><Rocket class="size-3.5" />{selected.run_count ? 'Resume' : 'Launch'}</Button>
                </form>
              {/if}
            </div>
          </div>
        {/if}
      {:else}
        <div class="flex min-h-80 flex-col items-center justify-center text-center">
          <Target class="size-8 text-[color:var(--text-subtle)]" />
          <p class="mt-3 text-sm font-medium">No outbound campaign yet</p>
          <p class="mt-1 max-w-sm text-xs text-[color:var(--text-muted)]">
            Start with a narrow ICP, then import a researched CSV prospect list.
          </p>
          <Button size="sm" class="mt-4" onclick={startCreate}><Plus class="size-3.5" />Create campaign</Button>
        </div>
      {/if}
    </section>
  </div>

  {#if selected && !creating}
    <section class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-5">
      <div class="flex flex-wrap items-start justify-between gap-4">
        <div>
          <div class="flex items-center gap-2">
            <FileSpreadsheet class="size-4 text-emerald-600" />
            <h2 class="text-sm font-semibold">Import prospect list</h2>
          </div>
          <p class="mt-1 text-xs text-[color:var(--text-muted)]">
            Up to 500 rows. Duplicates are checked within the file, every campaign, and existing CRM leads.
          </p>
        </div>
        <code class="rounded bg-[color:var(--bg-muted)] px-2 py-1 text-[10px] text-[color:var(--text-muted)]">
          company_name,email,first_name,last_name,website,country
        </code>
      </div>
      <form method="POST" action="?/importProspects" use:enhance class="mt-5">
        <input type="hidden" name="campaign_id" value={selected.id} />
        <textarea
          name="csv_text"
          bind:value={csvText}
          rows="8"
          required
          class="w-full rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg)] px-3 py-2 font-mono text-xs leading-5"
          placeholder={'company_name,email,first_name,last_name,job_title,linkedin_url,website,industry,country,source_url,notes\nAcme Robotics,ada@acme.example,Ada,Lovelace,CTO,https://linkedin.com/in/ada,https://acme.example,Robotics,US,https://source.example,Research note'}
        ></textarea>
        <div class="mt-3 flex flex-wrap items-center justify-between gap-3">
          <label class="flex items-center gap-2 text-xs text-[color:var(--text-muted)]">
            <input name="promote_ready" type="checkbox" class="size-4 rounded border-[color:var(--border-faint)]" />
            Queue every valid new prospect into the SDR pipeline immediately
          </label>
          <Button size="sm" type="submit"><Upload class="size-3.5" />Import CSV</Button>
        </div>
      </form>

      {#if form?.importResult}
        <div class="mt-5 grid gap-3 sm:grid-cols-4">
          {#each [
            { label: 'Created', value: form.importResult.created, tone: 'text-emerald-700' },
            { label: 'Queued', value: form.importResult.queued, tone: 'text-blue-700' },
            { label: 'Duplicates', value: form.importResult.duplicate_count, tone: 'text-amber-700' },
            { label: 'Errors', value: form.importResult.error_count, tone: 'text-red-700' }
          ] as item (item.label)}
            <div class="rounded-lg bg-[color:var(--bg-muted)] p-3">
              <p class="text-[11px] text-[color:var(--text-muted)]">{item.label}</p>
              <p class={`mt-1 text-lg font-semibold tabular-nums ${item.tone}`}>{item.value || 0}</p>
            </div>
          {/each}
        </div>
        {#if form.importResult.errors?.length || form.importResult.duplicates?.length}
          <div class="mt-3 max-h-36 overflow-y-auto rounded-lg border border-[color:var(--border-faint)] p-3 text-[11px] text-[color:var(--text-muted)]">
            {#each form.importResult.errors || [] as item}
              <p>Row {item.row}: {item.field} — {item.message}</p>
            {/each}
            {#each form.importResult.duplicates || [] as item}
              <p>Row {item.row}: {item.reason}</p>
            {/each}
          </div>
        {/if}
      {/if}
    </section>

    <section class="overflow-hidden rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)]">
      <div class="flex flex-wrap items-center justify-between gap-3 border-b border-[color:var(--border-faint)] p-5">
        <div>
          <h2 class="text-sm font-semibold">Prospects · {selected.name}</h2>
          <p class="mt-1 text-xs text-[color:var(--text-muted)]">
            {data.prospects?.count || 0} unique records · {prospectSummary.ready || 0} ready · {prospectSummary.promoted || 0} promoted
          </p>
        </div>
        <span class={`rounded-full px-2.5 py-1 text-[11px] font-medium ${statusClass(selected.status)}`}>
          {selected.status}
        </span>
      </div>
      {#if prospects.length}
        <div class="overflow-x-auto">
          <table class="w-full min-w-[980px] text-left text-xs">
            <thead class="bg-[color:var(--bg-muted)] text-[color:var(--text-muted)]">
              <tr>
                <th class="px-5 py-3 font-medium">Prospect</th>
                <th class="px-4 py-3 font-medium">Company</th>
                <th class="px-4 py-3 font-medium">Country</th>
                <th class="px-4 py-3 font-medium">Status</th>
                <th class="px-4 py-3 font-medium">Added</th>
                <th class="px-5 py-3 text-right font-medium">Action</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-[color:var(--border-faint)]">
              {#each prospects as prospect (prospect.id)}
                <tr class="hover:bg-[color:var(--bg-muted)]/50">
                  <td class="px-5 py-3">
                    <p class="font-medium text-[color:var(--text)]">{contactName(prospect)}</p>
                    <p class="mt-0.5 text-[11px] text-[color:var(--text-muted)]">{prospect.job_title || prospect.email || '-'}</p>
                  </td>
                  <td class="px-4 py-3">
                    <div class="flex items-center gap-2">
                      <Building2 class="size-3.5 text-[color:var(--text-subtle)]" />
                      <span>{prospect.company_name}</span>
                    </div>
                  </td>
                  <td class="px-4 py-3 text-[color:var(--text-muted)]">{prospect.country || '-'}</td>
                  <td class="px-4 py-3">
                    <span class={`rounded-full px-2 py-0.5 text-[10px] font-medium ${statusClass(prospect.status)}`}>
                      {prospect.status}
                    </span>
                    {#if prospect.last_error_message}
                      <p class="mt-1 max-w-48 truncate text-[10px] text-red-600" title={prospect.last_error_message}>{prospect.last_error_message}</p>
                    {/if}
                  </td>
                  <td class="px-4 py-3 text-[color:var(--text-muted)]">{formatDate(prospect.created_at)}</td>
                  <td class="px-5 py-3">
                    <div class="flex justify-end gap-1.5">
                      {#if prospect.status === 'ready' || prospect.status === 'failed'}
                        <form method="POST" action="?/prospectAction" use:enhance>
                          <input type="hidden" name="prospect_id" value={prospect.id} />
                          <input type="hidden" name="prospect_action" value="promote" />
                          <Button size="sm" variant="outline" type="submit">
                            {#if prospect.status === 'failed'}<RefreshCw class="size-3" />{:else}<Rocket class="size-3" />{/if}
                            {prospect.status === 'failed' ? 'Retry' : 'Promote'}
                          </Button>
                        </form>
                        <form method="POST" action="?/prospectAction" use:enhance>
                          <input type="hidden" name="prospect_id" value={prospect.id} />
                          <input type="hidden" name="prospect_action" value="disqualify" />
                          <Button size="sm" variant="ghost" type="submit"><Ban class="size-3" />Skip</Button>
                        </form>
                      {:else if prospect.status === 'disqualified'}
                        <form method="POST" action="?/prospectAction" use:enhance>
                          <input type="hidden" name="prospect_id" value={prospect.id} />
                          <input type="hidden" name="prospect_action" value="restore" />
                          <Button size="sm" variant="outline" type="submit"><RefreshCw class="size-3" />Restore</Button>
                        </form>
                      {:else if prospect.status === 'promoted' && prospect.lead_id}
                        <a href={`/leads/${prospect.lead_id}`} class="inline-flex items-center gap-1 text-xs font-medium text-violet-600 hover:underline">
                          Open lead <ArrowUpRight class="size-3" />
                        </a>
                      {:else}
                        <span class="inline-flex items-center gap-1 text-[11px] text-blue-600">
                          <LoaderCircle class="size-3 animate-spin" />Processing
                        </span>
                      {/if}
                    </div>
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {:else}
        <div class="p-10 text-center">
          <FileSpreadsheet class="mx-auto size-7 text-[color:var(--text-subtle)]" />
          <p class="mt-3 text-sm font-medium">No prospects in this campaign</p>
          <p class="mt-1 text-xs text-[color:var(--text-muted)]">Paste a researched CSV list above to begin cleaning and deduplication.</p>
        </div>
      {/if}
    </section>
  {/if}
</div>
