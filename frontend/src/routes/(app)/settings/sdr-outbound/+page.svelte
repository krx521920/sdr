<script>
  import { enhance } from '$app/forms';
  import { PageHeader } from '$lib/components/layout';
  import { LinkedinIcon as Linkedin } from '$lib/components/icons';
  import { Button } from '$lib/components/ui/button/index.js';
  import {
    AlertTriangle,
    ArrowUpRight,
    Ban,
    BarChart3,
    Building2,
    CheckCircle2,
    CircleDot,
    FileSpreadsheet,
    LoaderCircle,
    Mail,
    MessageCircle,
    Pause,
    Phone,
    Plus,
    RefreshCw,
    Rocket,
    Search,
    ShieldCheck,
    Sparkles,
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
  const whatsappConnection = $derived(form?.whatsappConnection || data.whatsappConnection || null);
  const apolloConnection = $derived(form?.apolloConnection || data.apolloConnection || null);
  const linkedinConnection = $derived(form?.linkedinConnection || data.linkedinConnection || null);
  const outboundSources = $derived(data.outboundSources || []);
  const copyDrafts = $derived(data.copyDrafts || []);
  const campaignAnalytics = $derived(data.campaignAnalytics || null);
  const cohortMetrics = $derived(campaignAnalytics?.cohort || {});
  const emailMetrics = $derived(campaignAnalytics?.email || {});
  const linkedinMetrics = $derived(campaignAnalytics?.linkedin || {});
  const whatsappMetrics = $derived(campaignAnalytics?.whatsapp || {});
  const latestCopyDraft = $derived(
    form?.outboundCopyApplyResult?.draft || form?.outboundCopyDraft || copyDrafts[0] || null
  );

  let creating = $state(false);
  let name = $state('');
  let description = $state('');
  let icpDescription = $state('');
  let status = $state('draft');
  let selectedChannels = $state([]);
  let sequenceId = $state('');
  let dailySendLimit = $state(50);
  let linkedinInvitationMessage = $state('');
  let linkedinActive = $state(false);
  let linkedinPartnerConfirmed = $state(false);
  let whatsappTemplateName = $state('');
  let whatsappTemplateLanguage = $state('en_US');
  let whatsappPhoneNumberId = $state('');
  let whatsappBusinessAccountId = $state('');
  let whatsappDisplayNumber = $state('');
  let whatsappActive = $state(false);
  let apolloActive = $state(false);
  let whatsappConfigTest = $state(null);
  let linkedinConfigTest = $state(null);
  let apolloConfigTest = $state(null);
  let csvText = $state('');
  const prospectCsvPlaceholder =
    'company_name,email,phone,first_name,last_name,job_title,linkedin_url,website,industry,country,recipient_timezone,source_url,notes,lawful_basis,lawful_basis_notes,consent_at,consent_evidence,allowed_channels\nAcme Robotics,ada@acme.example,+15551234567,Ada,Lovelace,CTO,https://linkedin.com/in/ada,https://acme.example,Robotics,US,America/New_York,https://source.example,Research note,legitimate_interest,Review LI-42,,,email|linkedin';

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
      linkedinInvitationMessage = campaign.linkedin_invitation_message || '';
      whatsappTemplateName = campaign.whatsapp_template_name || '';
      whatsappTemplateLanguage = campaign.whatsapp_template_language || 'en_US';
    }
  });

  $effect(() => {
    apolloActive = Boolean(apolloConnection?.is_active);
  });

  $effect(() => {
    const connection = linkedinConnection;
    if (connection) {
      linkedinActive = Boolean(connection.is_active);
      linkedinPartnerConfirmed = Boolean(connection.partner_access_confirmed);
    }
  });

  $effect(() => {
    const connection = whatsappConnection;
    if (connection) {
      whatsappPhoneNumberId = connection.phone_number_id || '';
      whatsappBusinessAccountId = connection.business_account_id || '';
      whatsappDisplayNumber = connection.display_phone_number || '';
      whatsappActive = Boolean(connection.is_active);
    }
  });

  $effect(() => {
    if (form?.actionError) toast.error(form.actionError);
    if (form?.campaignSaved) {
      toast.success('Outbound campaign saved.');
      creating = false;
    }
    if (form?.whatsappSaved) {
      whatsappConfigTest = null;
      toast.success('WhatsApp Business connection saved.');
    }
    if (form?.linkedinSaved) {
      linkedinConfigTest = null;
      toast.success('LinkedIn partner connection saved.');
    }
    if (form?.apolloSaved) {
      apolloConfigTest = null;
      toast.success('Apollo connection saved.');
    }
    if (form?.whatsappConfigTest) {
      whatsappConfigTest = form.whatsappConfigTest;
      if (form.whatsappConfigTest.ok) toast.success('WhatsApp local configuration is ready.');
    }
    if (form?.linkedinConfigTest) {
      linkedinConfigTest = form.linkedinConfigTest;
      if (form.linkedinConfigTest.ok) toast.success('LinkedIn local configuration is ready.');
    }
    if (form?.apolloConfigTest) {
      apolloConfigTest = form.apolloConfigTest;
      if (form.apolloConfigTest.ok) toast.success('Apollo local configuration is ready.');
    }
    if (form?.apolloSourceSaved) toast.success('Apollo prospect source saved.');
    if (form?.apolloSourceQueued) toast.success('Apollo source sync queued.');
    if (form?.outboundCopyQueued) toast.success('AI copy generation queued for review.');
    if (form?.outboundCopySaved) toast.success('Reviewed outbound copy saved.');
    if (form?.outboundCopyApplied) {
      toast.success('Reviewed copy applied to an inactive outbound sequence.');
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
    selectedChannels = ['email'];
    sequenceId = '';
    dailySendLimit = 50;
    linkedinInvitationMessage = '';
    whatsappTemplateName = '';
    whatsappTemplateLanguage = 'en_US';
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

  /** @param {number | string | null | undefined} value */
  function formatPercent(value) {
    return `${Number(value || 0).toFixed(1)}%`;
  }

  /** @param {{ code?: string } | null} result */
  function localConnectionTestMessage(result) {
    /** @type {Record<string, string>} */
    const messages = {
      connection_ready: 'Saved configuration is locally complete.',
      connection_missing: 'Save this connection before checking it.',
      connection_inactive: 'Enable and save this connection before checking it.',
      required_identifier_missing: 'A required saved provider identifier is missing.',
      credential_missing: 'No saved credential is available.',
      credential_decryption_failed: 'The saved credential cannot be read. Save it again.',
      partner_access_not_confirmed: 'Confirm and save approved LinkedIn partner access first.',
      permission_denied: 'Organization administrator access is required.'
    };
    return messages[result?.code] || 'The saved configuration could not be checked.';
  }

  /** @param {any} prospect */
  function contactName(prospect) {
    return (
      [prospect.first_name, prospect.last_name].filter(Boolean).join(' ') ||
      prospect.email ||
      'Company prospect'
    );
  }

  /** @param {string} channel */
  function channelIcon(channel) {
    if (channel === 'email') return Mail;
    if (channel === 'linkedin') return Linkedin;
    if (channel === 'whatsapp') return MessageCircle;
    if (channel === 'phone') return Phone;
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

  <section
    class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-5"
  >
    <div class="flex flex-wrap items-start justify-between gap-4">
      <div>
        <div class="flex items-center gap-2">
          <MessageCircle class="size-4 text-emerald-600" />
          <h2 class="text-sm font-semibold">WhatsApp Business sender</h2>
          <span
            class={`rounded-full px-2 py-0.5 text-[10px] font-medium ${whatsappActive ? 'bg-emerald-100 text-emerald-700' : 'bg-slate-100 text-slate-600'}`}
          >
            {whatsappActive ? 'Active' : 'Inactive'}
          </span>
        </div>
        <p class="mt-1 text-xs text-[color:var(--text-muted)]">
          Official Meta Cloud API credentials. The access token is encrypted and never shown again.
        </p>
      </div>
      {#if whatsappConnection?.message_summary}
        <div class="flex gap-4 text-right text-[11px] text-[color:var(--text-muted)]">
          <span
            ><strong class="block text-sm text-[color:var(--text)]"
              >{whatsappConnection.message_summary.sent || 0}</strong
            >sent</span
          >
          <span
            ><strong class="block text-sm text-emerald-700"
              >{whatsappConnection.message_summary.delivered || 0}</strong
            >delivered</span
          >
          <span
            ><strong class="block text-sm text-red-700"
              >{whatsappConnection.message_summary.failed || 0}</strong
            >failed</span
          >
        </div>
      {/if}
    </div>
    {#if data.whatsappError}
      <p class="mt-3 rounded-md bg-amber-50 px-3 py-2 text-xs text-amber-800">
        {data.whatsappError}
      </p>
    {/if}
    <form
      method="POST"
      action="?/saveWhatsAppConnection"
      use:enhance
      class="mt-4 grid gap-3 lg:grid-cols-4"
    >
      <label class="space-y-1.5 text-xs font-medium">
        Phone Number ID
        <input
          name="phone_number_id"
          bind:value={whatsappPhoneNumberId}
          required
          inputmode="numeric"
          pattern="[0-9]+"
          class="h-9 w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg)] px-3 text-sm font-normal"
        />
      </label>
      <label class="space-y-1.5 text-xs font-medium">
        Business Account ID
        <input
          name="business_account_id"
          bind:value={whatsappBusinessAccountId}
          inputmode="numeric"
          pattern="[0-9]*"
          class="h-9 w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg)] px-3 text-sm font-normal"
        />
      </label>
      <label class="space-y-1.5 text-xs font-medium">
        Display number
        <input
          name="display_phone_number"
          bind:value={whatsappDisplayNumber}
          placeholder="+1 555 123 4567"
          class="h-9 w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg)] px-3 text-sm font-normal"
        />
      </label>
      <label class="space-y-1.5 text-xs font-medium">
        System-user token
        <input
          name="access_token"
          type="password"
          autocomplete="new-password"
          placeholder={whatsappConnection?.access_token_configured
            ? `Configured ···${whatsappConnection.access_token_hint || ''}`
            : 'Required'}
          class="h-9 w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg)] px-3 text-sm font-normal"
        />
      </label>
      <div class="flex items-center justify-between gap-4 lg:col-span-4">
        <label class="flex items-center gap-2 text-xs">
          <input
            name="is_active"
            type="checkbox"
            bind:checked={whatsappActive}
            class="size-4 rounded border-[color:var(--border-faint)]"
          />
          Enable campaign sending
        </label>
        <Button size="sm" variant="outline" type="submit">Save WhatsApp connection</Button>
      </div>
    </form>
    <div
      class="mt-3 flex flex-wrap items-center justify-between gap-3 border-t border-[color:var(--border-faint)] pt-3"
    >
      <div>
        <p class="text-xs font-medium">Local configuration check</p>
        <p class="mt-0.5 text-[11px] text-[color:var(--text-muted)]">
          Checks saved values only. No external provider request is made and nothing is sent.
        </p>
      </div>
      <form method="POST" action="?/testWhatsAppConnection" use:enhance>
        <Button size="sm" variant="outline" type="submit">
          <ShieldCheck class="size-3.5" />
          Check local configuration
        </Button>
      </form>
    </div>
    {#if whatsappConfigTest}
      <div
        aria-live="polite"
        class={`mt-3 flex gap-2 rounded-md px-3 py-2 text-xs ${whatsappConfigTest.ok ? 'bg-emerald-50 text-emerald-800' : 'bg-amber-50 text-amber-800'}`}
      >
        {#if whatsappConfigTest.ok}
          <CheckCircle2 class="mt-0.5 size-3.5 shrink-0" />
        {:else}
          <AlertTriangle class="mt-0.5 size-3.5 shrink-0" />
        {/if}
        <span
          >{localConnectionTestMessage(whatsappConfigTest)} No external provider call or message was sent.</span
        >
      </div>
    {/if}
  </section>

  <section
    class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-5"
  >
    <div class="flex flex-wrap items-start justify-between gap-4">
      <div>
        <div class="flex items-center gap-2">
          <Linkedin class="size-4 text-blue-700" />
          <h2 class="text-sm font-semibold">LinkedIn partner invitations</h2>
          <span
            class={`rounded-full px-2 py-0.5 text-[10px] font-medium ${linkedinActive ? 'bg-emerald-100 text-emerald-700' : 'bg-slate-100 text-slate-600'}`}
          >
            {linkedinActive ? 'Active' : 'Inactive'}
          </span>
        </div>
        <p class="mt-1 max-w-3xl text-xs text-[color:var(--text-muted)]">
          Uses LinkedIn's official Invitations API. It is restricted to approved partners; personal
          session cookies and browser automation are intentionally unsupported.
          <a
            href="https://learn.microsoft.com/en-us/linkedin/shared/integrations/communications/invitations"
            target="_blank"
            rel="noreferrer"
            class="font-medium text-blue-700 underline">Review access requirements</a
          >.
        </p>
      </div>
      {#if linkedinConnection?.invitation_summary}
        <div class="flex gap-4 text-right text-[11px] text-[color:var(--text-muted)]">
          <span
            ><strong class="block text-sm text-[color:var(--text)]"
              >{linkedinConnection.invitation_summary.sent || 0}</strong
            >sent</span
          >
          <span
            ><strong class="block text-sm text-red-700"
              >{linkedinConnection.invitation_summary.failed || 0}</strong
            >failed</span
          >
        </div>
      {/if}
    </div>
    {#if data.linkedinError}
      <p class="mt-3 rounded-md bg-amber-50 px-3 py-2 text-xs text-amber-800">
        {data.linkedinError}
      </p>
    {/if}
    <form
      method="POST"
      action="?/saveLinkedInConnection"
      use:enhance
      class="mt-4 grid gap-3 lg:grid-cols-[minmax(260px,1fr)_minmax(260px,1fr)_auto] lg:items-end"
    >
      <label class="space-y-1.5 text-xs font-medium">
        Partner API access token
        <input
          name="access_token"
          type="password"
          autocomplete="new-password"
          placeholder={linkedinConnection?.access_token_configured
            ? `Configured ···${linkedinConnection.access_token_hint || ''}`
            : 'Required for approved partners'}
          class="h-9 w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg)] px-3 text-sm font-normal"
        />
      </label>
      <div class="space-y-2 pb-0.5 text-xs">
        <label class="flex items-start gap-2">
          <input
            name="partner_access_confirmed"
            type="checkbox"
            bind:checked={linkedinPartnerConfirmed}
            class="mt-0.5 size-4 rounded border-[color:var(--border-faint)]"
          />
          I confirm this organization has approved LinkedIn Invitations API access
        </label>
        <label class="flex items-center gap-2">
          <input
            name="is_active"
            type="checkbox"
            bind:checked={linkedinActive}
            class="size-4 rounded border-[color:var(--border-faint)]"
          />
          Enable campaign invitations
        </label>
      </div>
      <Button size="sm" variant="outline" type="submit">Save LinkedIn connection</Button>
    </form>
    <div
      class="mt-3 flex flex-wrap items-center justify-between gap-3 border-t border-[color:var(--border-faint)] pt-3"
    >
      <div>
        <p class="text-xs font-medium">Local configuration check</p>
        <p class="mt-0.5 text-[11px] text-[color:var(--text-muted)]">
          Checks saved values only. No external provider request is made and nothing is sent.
        </p>
      </div>
      <form method="POST" action="?/testLinkedInConnection" use:enhance>
        <Button size="sm" variant="outline" type="submit">
          <ShieldCheck class="size-3.5" />
          Check local configuration
        </Button>
      </form>
    </div>
    {#if linkedinConfigTest}
      <div
        aria-live="polite"
        class={`mt-3 flex gap-2 rounded-md px-3 py-2 text-xs ${linkedinConfigTest.ok ? 'bg-emerald-50 text-emerald-800' : 'bg-amber-50 text-amber-800'}`}
      >
        {#if linkedinConfigTest.ok}
          <CheckCircle2 class="mt-0.5 size-3.5 shrink-0" />
        {:else}
          <AlertTriangle class="mt-0.5 size-3.5 shrink-0" />
        {/if}
        <span
          >{localConnectionTestMessage(linkedinConfigTest)} No external provider call or message was sent.</span
        >
      </div>
    {/if}
  </section>

  <section
    class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-5"
  >
    <div class="flex flex-wrap items-start justify-between gap-4">
      <div>
        <div class="flex items-center gap-2">
          <Search class="size-4 text-violet-600" />
          <h2 class="text-sm font-semibold">Apollo prospect source</h2>
          <span
            class={`rounded-full px-2 py-0.5 text-[10px] font-medium ${apolloActive ? 'bg-emerald-100 text-emerald-700' : 'bg-slate-100 text-slate-600'}`}
          >
            {apolloActive ? 'Active' : 'Inactive'}
          </span>
        </div>
        <p class="mt-1 text-xs text-[color:var(--text-muted)]">
          People Search is free. Each selected person is enriched before import and can consume
          Apollo credits; the API key is encrypted and never shown again.
        </p>
      </div>
      {#if apolloConnection?.last_sync_at}
        <p class="text-[11px] text-[color:var(--text-muted)]">
          Last successful sync {formatDate(apolloConnection.last_sync_at)}
        </p>
      {/if}
    </div>
    {#if data.apolloError}
      <p class="mt-3 rounded-md bg-amber-50 px-3 py-2 text-xs text-amber-800">
        {data.apolloError}
      </p>
    {/if}
    <form
      method="POST"
      action="?/saveApolloConnection"
      use:enhance
      class="mt-4 flex flex-wrap items-end gap-3"
    >
      <label class="min-w-64 flex-1 space-y-1.5 text-xs font-medium">
        Apollo API key
        <input
          name="api_key"
          type="password"
          autocomplete="new-password"
          placeholder={apolloConnection?.api_key_configured
            ? `Configured …${apolloConnection.api_key_hint || ''}`
            : 'Required'}
          class="h-9 w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg)] px-3 text-sm font-normal"
        />
      </label>
      <label class="flex h-9 items-center gap-2 text-xs">
        <input
          name="is_active"
          type="checkbox"
          bind:checked={apolloActive}
          class="size-4 rounded border-[color:var(--border-faint)]"
        />
        Enable automatic sources
      </label>
      <Button size="sm" variant="outline" type="submit">Save Apollo connection</Button>
    </form>
    <div
      class="mt-3 flex flex-wrap items-center justify-between gap-3 border-t border-[color:var(--border-faint)] pt-3"
    >
      <div>
        <p class="text-xs font-medium">Local configuration check</p>
        <p class="mt-0.5 text-[11px] text-[color:var(--text-muted)]">
          Checks saved values only. No external provider request is made and nothing is sent.
        </p>
      </div>
      <form method="POST" action="?/testApolloConnection" use:enhance>
        <Button size="sm" variant="outline" type="submit">
          <ShieldCheck class="size-3.5" />
          Check local configuration
        </Button>
      </form>
    </div>
    {#if apolloConfigTest}
      <div
        aria-live="polite"
        class={`mt-3 flex gap-2 rounded-md px-3 py-2 text-xs ${apolloConfigTest.ok ? 'bg-emerald-50 text-emerald-800' : 'bg-amber-50 text-amber-800'}`}
      >
        {#if apolloConfigTest.ok}
          <CheckCircle2 class="mt-0.5 size-3.5 shrink-0" />
        {:else}
          <AlertTriangle class="mt-0.5 size-3.5 shrink-0" />
        {/if}
        <span
          >{localConnectionTestMessage(apolloConfigTest)} No external provider call or message was sent.</span
        >
      </div>
    {/if}
  </section>

  <section class="grid gap-4 sm:grid-cols-2 xl:grid-cols-5">
    {#each [{ label: 'Campaigns', value: summary.campaigns || 0, icon: Target, color: 'text-violet-600' }, { label: 'Active', value: summary.active_campaigns || 0, icon: Rocket, color: 'text-blue-600' }, { label: 'Prospects', value: summary.prospects || 0, icon: Users, color: 'text-slate-600' }, { label: 'Ready', value: summary.ready || 0, icon: CircleDot, color: 'text-amber-600' }, { label: 'Promoted', value: summary.promoted || 0, icon: CheckCircle2, color: 'text-emerald-600' }] as card (card.label)}
      <div
        class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-4"
      >
        <div class="flex items-center justify-between">
          <p class="text-xs text-[color:var(--text-muted)]">{card.label}</p>
          <card.icon class={`size-4 ${card.color}`} />
        </div>
        <p class="mt-2 text-2xl font-semibold tabular-nums">{card.value}</p>
      </div>
    {/each}
  </section>

  <div class="grid gap-6 xl:grid-cols-[320px_minmax(0,1fr)]">
    <section
      class="overflow-hidden rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)]"
    >
      <div class="border-b border-[color:var(--border-faint)] p-4">
        <h2 class="text-sm font-semibold">Campaigns</h2>
        <p class="mt-1 text-xs text-[color:var(--text-muted)]">One ICP and channel mix per list</p>
      </div>
      <div class="max-h-[620px] divide-y divide-[color:var(--border-faint)] overflow-y-auto">
        {#each campaigns as campaign (campaign.id)}
          <a
            href={`?campaign=${campaign.id}`}
            class="block p-4 transition-colors hover:bg-[color:var(--bg-muted)] {selected?.id ===
              campaign.id && !creating
              ? 'bg-[color:var(--bg-muted)]'
              : ''}"
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
              <span
                class={`rounded-full px-2 py-0.5 text-[10px] font-medium ${statusClass(campaign.status)}`}
              >
                {campaign.status}
              </span>
            </div>
            {#if campaign.channels?.length}
              <div class="mt-3 flex items-center gap-1.5">
                {#each campaign.channels as channel (channel)}
                  {@const Icon = channelIcon(channel)}
                  <span
                    class="flex size-6 items-center justify-center rounded bg-[color:var(--bg-elevated)] text-[color:var(--text-muted)]"
                    title={channel}
                  >
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

    <section
      class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-5"
    >
      <div class="flex flex-wrap items-start justify-between gap-4">
        <div>
          <div class="flex items-center gap-2">
            <Sparkles class="size-4 text-fuchsia-600" />
            <h2 class="text-sm font-semibold">AI outbound copy studio</h2>
            {#if latestCopyDraft}
              <span
                class={`rounded-full px-2 py-0.5 text-[10px] font-medium ${statusClass(latestCopyDraft.status)}`}
              >
                {latestCopyDraft.status}
              </span>
            {/if}
          </div>
          <p class="mt-1 text-xs text-[color:var(--text-muted)]">
            Generate structured Subject, Opening, CTA and A/B variants. Nothing changes a Sequence
            until an administrator reviews and applies the draft.
          </p>
        </div>
        {#if latestCopyDraft?.provider}
          <p class="text-[11px] text-[color:var(--text-muted)]">
            {latestCopyDraft.provider}:{latestCopyDraft.model} ·
            {(latestCopyDraft.input_tokens || 0) + (latestCopyDraft.output_tokens || 0)} tokens
          </p>
        {/if}
      </div>

      <form method="POST" action="?/generateOutboundCopy" use:enhance class="mt-5 space-y-4">
        <input type="hidden" name="campaign_id" value={selected.id} />
        <div class="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
          <label class="space-y-1.5 text-xs font-medium xl:col-span-2">
            Offering summary
            <textarea
              name="offering_summary"
              rows="2"
              required
              maxlength="4000"
              placeholder="What the product does, for whom, and the operational problem it solves."
              class="w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg)] px-3 py-2 text-sm font-normal"
            ></textarea>
          </label>
          <label class="space-y-1.5 text-xs font-medium xl:col-span-2">
            Value proposition
            <textarea
              name="value_proposition"
              rows="2"
              required
              maxlength="4000"
              placeholder="The concrete buyer outcome, without unsupported claims."
              class="w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg)] px-3 py-2 text-sm font-normal"
            ></textarea>
          </label>
          <label class="space-y-1.5 text-xs font-medium xl:col-span-2">
            Approved proof points
            <textarea
              name="proof_points"
              rows="2"
              maxlength="4000"
              placeholder="Only verified customers, metrics or evidence. Leave blank when unavailable."
              class="w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg)] px-3 py-2 text-sm font-normal"
            ></textarea>
          </label>
          <label class="space-y-1.5 text-xs font-medium xl:col-span-2">
            CTA goal
            <input
              name="cta_goal"
              required
              maxlength="500"
              placeholder="Ask permission to share a two-minute workflow comparison"
              class="h-9 w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg)] px-3 text-sm font-normal"
            />
          </label>
          <label class="space-y-1.5 text-xs font-medium">
            Language
            <input
              name="language"
              value="English"
              maxlength="40"
              class="h-9 w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg)] px-3 text-sm font-normal"
            />
          </label>
          <label class="space-y-1.5 text-xs font-medium">
            Tone
            <input
              name="tone"
              value="concise and consultative"
              maxlength="80"
              class="h-9 w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg)] px-3 text-sm font-normal"
            />
          </label>
          <label class="space-y-1.5 text-xs font-medium">
            Steps
            <input
              name="step_count"
              type="number"
              min="1"
              max="5"
              value="3"
              class="h-9 w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg)] px-3 text-sm font-normal"
            />
          </label>
          <div class="flex items-end justify-end">
            <Button size="sm" type="submit"
              ><Sparkles class="size-3.5" />Generate review draft</Button
            >
          </div>
        </div>
      </form>

      {#if latestCopyDraft?.status === 'pending' || latestCopyDraft?.status === 'generating'}
        <div
          class="mt-5 flex items-center justify-between rounded-lg bg-blue-50 px-4 py-3 text-xs text-blue-800"
        >
          <span class="flex items-center gap-2"
            ><LoaderCircle class="size-4 animate-spin" />The durable AI job is running.</span
          >
          <a href={`?campaign=${selected.id}`} class="font-medium underline">Refresh status</a>
        </div>
      {:else if latestCopyDraft?.status === 'failed'}
        <p class="mt-5 rounded-lg bg-red-50 px-4 py-3 text-xs text-red-800">
          {latestCopyDraft.error_message || 'Copy generation failed.'}
        </p>
      {:else if latestCopyDraft?.generated_steps?.length}
        <form method="POST" action="?/saveOutboundCopy" use:enhance class="mt-5 space-y-4">
          <input type="hidden" name="draft_id" value={latestCopyDraft.id} />
          <input type="hidden" name="step_count" value={latestCopyDraft.step_count} />
          {#each latestCopyDraft.generated_steps as step (step.position)}
            <fieldset
              class="rounded-lg border border-[color:var(--border-faint)] p-4"
              disabled={latestCopyDraft.status === 'applied'}
            >
              <div class="flex items-center justify-between gap-3">
                <legend class="text-sm font-semibold">Step {step.position}</legend>
                <label class="flex items-center gap-2 text-xs">
                  Delay days
                  <input
                    name={`delay_days_${step.position}`}
                    type="number"
                    min="0"
                    max="30"
                    value={step.delay_days}
                    readonly={step.position === 1}
                    class="h-8 w-20 rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg)] px-2 text-sm font-normal"
                  />
                </label>
              </div>
              <div class="mt-3 grid gap-4 xl:grid-cols-2">
                {#each ['a', 'b'] as variant}
                  <div class="space-y-3 rounded-lg bg-[color:var(--bg-muted)] p-3">
                    <p class="text-xs font-semibold text-violet-700 uppercase">Variant {variant}</p>
                    <label class="block space-y-1 text-[11px] font-medium">
                      Subject
                      <input
                        name={`subject_${variant}_${step.position}`}
                        value={step[`subject_${variant}`]}
                        required
                        maxlength="255"
                        class="h-8 w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg)] px-2 text-xs font-normal"
                      />
                    </label>
                    <label class="block space-y-1 text-[11px] font-medium">
                      Opening
                      <textarea
                        name={`opening_${variant}_${step.position}`}
                        rows="2"
                        required
                        maxlength="500"
                        class="w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg)] px-2 py-1.5 text-xs font-normal"
                        >{step[`opening_${variant}`]}</textarea
                      >
                    </label>
                    <label class="block space-y-1 text-[11px] font-medium">
                      Complete email body
                      <textarea
                        name={`body_${variant}_${step.position}`}
                        rows="6"
                        required
                        maxlength="4000"
                        class="w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg)] px-2 py-1.5 text-xs leading-5 font-normal"
                        >{step[`body_${variant}`]}</textarea
                      >
                    </label>
                    <label class="block space-y-1 text-[11px] font-medium">
                      CTA
                      <input
                        name={`cta_${variant}_${step.position}`}
                        value={step[`cta_${variant}`]}
                        required
                        maxlength="500"
                        class="h-8 w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg)] px-2 text-xs font-normal"
                      />
                    </label>
                  </div>
                {/each}
              </div>
              <label class="mt-3 block space-y-1 text-[11px] font-medium">
                Why these variants
                <input
                  name={`rationale_${step.position}`}
                  value={step.rationale}
                  required
                  maxlength="1000"
                  class="h-8 w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg)] px-2 text-xs font-normal"
                />
              </label>
            </fieldset>
          {/each}
          {#if latestCopyDraft.status === 'ready'}
            <div class="flex justify-end">
              <Button size="sm" variant="outline" type="submit"
                ><ShieldCheck class="size-3.5" />Save human review</Button
              >
            </div>
          {:else}
            <p class="rounded-md bg-emerald-50 px-3 py-2 text-xs text-emerald-800">
              Applied to {form?.outboundCopyApplyResult?.sequence_name || 'an inactive sequence'}.
              Configure its sender and enable it manually before launching.
            </p>
          {/if}
        </form>
        {#if latestCopyDraft.status === 'ready'}
          <form
            method="POST"
            action="?/applyOutboundCopy"
            use:enhance
            class="mt-3 flex justify-end"
          >
            <input type="hidden" name="draft_id" value={latestCopyDraft.id} />
            <Button size="sm" type="submit"
              ><ShieldCheck class="size-3.5" />Approve and apply to Sequence</Button
            >
          </form>
        {/if}
      {/if}
    </section>

    <section
      class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-5"
    >
      {#if creating || selected}
        <div class="flex items-start justify-between gap-4">
          <div>
            <h2 class="text-sm font-semibold">
              {creating ? 'New outbound campaign' : 'Campaign brief'}
            </h2>
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
          {#if selectedChannels.includes('linkedin')}
            <div class="space-y-2">
              <label class="block space-y-1.5 text-xs font-medium">
                LinkedIn invitation note
                <textarea
                  name="linkedin_invitation_message"
                  bind:value={linkedinInvitationMessage}
                  rows="2"
                  maxlength="300"
                  class="w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg)] px-3 py-2 text-sm font-normal"
                  placeholder={'Hi {{ first_name }}, I would like to connect and learn more about {{ company_name }}.'}
                ></textarea>
              </label>
              <div
                class="flex flex-wrap items-center justify-between gap-2 text-[10px] text-[color:var(--text-muted)]"
              >
                <span>
                  Optional · variables: first_name, last_name, full_name, company_name, job_title
                </span>
                <span>{linkedinInvitationMessage.length}/300 before personalization</span>
              </div>
              {#if !linkedinConnection?.is_active || !linkedinConnection?.partner_access_confirmed}
                <p class="rounded-md bg-amber-50 px-3 py-2 text-xs text-amber-800">
                  Configure an enabled, approved LinkedIn partner connection above before launching
                  this campaign.
                </p>
              {/if}
            </div>
          {:else}
            <input
              type="hidden"
              name="linkedin_invitation_message"
              value={linkedinInvitationMessage}
            />
          {/if}
          {#if selectedChannels.includes('whatsapp')}
            <div class="grid gap-4 md:grid-cols-[minmax(0,1fr)_180px]">
              <label class="space-y-1.5 text-xs font-medium">
                Approved WhatsApp template
                <input
                  name="whatsapp_template_name"
                  bind:value={whatsappTemplateName}
                  required
                  pattern="[a-z0-9_]+"
                  maxlength="512"
                  class="h-9 w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg)] px-3 text-sm font-normal"
                  placeholder="industrial_intro"
                />
              </label>
              <label class="space-y-1.5 text-xs font-medium">
                Template language
                <input
                  name="whatsapp_template_language"
                  bind:value={whatsappTemplateLanguage}
                  required
                  pattern={'[a-z]{2,3}(_[A-Z]{2})?'}
                  maxlength="20"
                  class="h-9 w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg)] px-3 text-sm font-normal"
                  placeholder="en_US"
                />
              </label>
              {#if !whatsappConnection?.is_active}
                <p class="rounded-md bg-amber-50 px-3 py-2 text-xs text-amber-800 md:col-span-2">
                  Configure and enable the WhatsApp Business sender above before launching this
                  campaign.
                </p>
              {/if}
            </div>
          {:else}
            <input type="hidden" name="whatsapp_template_name" value={whatsappTemplateName} />
            <input
              type="hidden"
              name="whatsapp_template_language"
              value={whatsappTemplateLanguage}
            />
          {/if}
          {#if !outboundSequences.length}
            <p class="rounded-md bg-amber-50 px-3 py-2 text-xs text-amber-800">
              Create and enable a nurture sequence that explicitly includes the outbound source
              before launch.
              <a href="/settings/sdr-nurturing" class="font-medium underline">Configure sequences</a
              >
            </p>
          {/if}
          <label class="block space-y-1.5 text-xs font-medium">
            ICP definition
            <textarea
              name="icp_description"
              bind:value={icpDescription}
              rows="3"
              class="w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg)] px-3 py-2 text-sm font-normal"
              placeholder="Industry × company size × role × pain point × geography"></textarea>
          </label>
          <label class="block space-y-1.5 text-xs font-medium">
            Notes
            <textarea
              name="description"
              bind:value={description}
              rows="2"
              class="w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg)] px-3 py-2 text-sm font-normal"
              placeholder="Value proposition, exclusions, and list-building notes"></textarea>
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
                  <span
                    class="flex items-center gap-1.5 rounded-md border border-[color:var(--border-faint)] px-3 py-2 text-xs text-[color:var(--text-muted)] capitalize peer-checked:border-violet-300 peer-checked:bg-violet-50 peer-checked:text-violet-700"
                  >
                    <Icon class="size-3.5" />
                    {channel}
                  </span>
                </label>
              {/each}
            </div>
          </fieldset>
          <div class="flex justify-end">
            <Button size="sm" type="submit">{creating ? 'Create campaign' : 'Save campaign'}</Button
            >
          </div>
        </form>
        {#if !creating && selected}
          <div
            class="mt-4 flex flex-wrap items-center justify-between gap-3 border-t border-[color:var(--border-faint)] pt-4"
          >
            <div class="text-[11px] text-[color:var(--text-muted)]">
              Run {selected.run_count || 0} · limit {selected.daily_send_limit || 50}/day
              {#if selected.last_refilled_at}
                · last refill {formatDate(selected.last_refilled_at)}{/if}
            </div>
            <div class="flex flex-wrap gap-2">
              {#if selected.status === 'active'}
                {#if (selected.metrics?.failed || 0) > 0 || selected.channels?.includes('whatsapp') || selected.channels?.includes('linkedin')}
                  <form method="POST" action="?/campaignAction" use:enhance>
                    <input type="hidden" name="campaign_id" value={selected.id} />
                    <input type="hidden" name="campaign_action" value="retry_failed" />
                    <Button size="sm" variant="outline" type="submit"
                      ><RefreshCw class="size-3.5" />Retry failed</Button
                    >
                  </form>
                {/if}
                <form method="POST" action="?/campaignAction" use:enhance>
                  <input type="hidden" name="campaign_id" value={selected.id} />
                  <input type="hidden" name="campaign_action" value="pause" />
                  <Button size="sm" variant="outline" type="submit"
                    ><Pause class="size-3.5" />Pause</Button
                  >
                </form>
              {:else if selected.status !== 'archived'}
                <form method="POST" action="?/campaignAction" use:enhance>
                  <input type="hidden" name="campaign_id" value={selected.id} />
                  <input type="hidden" name="campaign_action" value="launch" />
                  <Button size="sm" type="submit"
                    ><Rocket class="size-3.5" />{selected.run_count ? 'Resume' : 'Launch'}</Button
                  >
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
          <Button size="sm" class="mt-4" onclick={startCreate}
            ><Plus class="size-3.5" />Create campaign</Button
          >
        </div>
      {/if}
    </section>
  </div>

  {#if selected && !creating && campaignAnalytics}
    <section
      class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-5"
    >
      <div class="flex flex-wrap items-start justify-between gap-4">
        <div>
          <div class="flex items-center gap-2">
            <BarChart3 class="size-4 text-blue-600" />
            <h2 class="text-sm font-semibold">Campaign & ICP performance</h2>
          </div>
          <p class="mt-1 text-xs text-[color:var(--text-muted)]">
            Lifetime attribution follows each prospect into its intake and delivery history, so
            shared Sequences cannot mix Campaign results.
          </p>
        </div>
        <a
          href={`?campaign=${selected.id}`}
          class="flex items-center gap-1 text-xs font-medium text-blue-700 hover:underline"
        >
          <RefreshCw class="size-3.5" />Refresh metrics
        </a>
      </div>

      <div class="mt-5 grid gap-3 sm:grid-cols-2 lg:grid-cols-4 xl:grid-cols-8">
        {#each [{ label: 'Prospects', value: cohortMetrics.prospects || 0, detail: `${formatPercent(cohortMetrics.promotion_rate)} promoted` }, { label: 'Email sent', value: emailMetrics.sent || 0, detail: `${emailMetrics.failed || 0} failed` }, { label: 'Opened', value: emailMetrics.opened || 0, detail: formatPercent(emailMetrics.open_rate) }, { label: 'Clicked', value: emailMetrics.clicked || 0, detail: formatPercent(emailMetrics.click_rate) }, { label: 'Replied', value: emailMetrics.replied || 0, detail: formatPercent(emailMetrics.reply_rate) }, { label: 'MQL', value: cohortMetrics.mql || 0, detail: formatPercent(cohortMetrics.mql_rate) }, { label: 'SQL', value: cohortMetrics.sql || 0, detail: formatPercent(cohortMetrics.sql_rate) }, { label: 'Bounced', value: emailMetrics.bounced || 0, detail: formatPercent(emailMetrics.bounce_rate) }] as metric (metric.label)}
          <div class="rounded-lg bg-[color:var(--bg-muted)] p-3">
            <p
              class="text-[10px] font-medium tracking-wide text-[color:var(--text-muted)] uppercase"
            >
              {metric.label}
            </p>
            <p class="mt-1 text-xl font-semibold tabular-nums">{metric.value}</p>
            <p class="mt-1 text-[10px] text-[color:var(--text-muted)]">{metric.detail}</p>
          </div>
        {/each}
      </div>

      <div class="mt-5 grid gap-4 xl:grid-cols-[minmax(0,2fr)_minmax(260px,1fr)]">
        <div class="overflow-hidden rounded-lg border border-[color:var(--border-faint)]">
          <div
            class="flex items-center justify-between border-b border-[color:var(--border-faint)] px-4 py-3"
          >
            <div>
              <h3 class="text-xs font-semibold">Email steps and A/B outcomes</h3>
              <p class="mt-0.5 text-[10px] text-[color:var(--text-muted)]">
                A/B shows sent volume; engagement rates use all variants at that step.
              </p>
            </div>
            <span class="text-[10px] text-[color:var(--text-muted)]">
              {emailMetrics.delivered || 0} delivered · {emailMetrics.positive_replies || 0}
              positive
            </span>
          </div>
          <div class="overflow-x-auto">
            <table class="w-full min-w-[720px] text-left text-xs">
              <thead
                class="bg-[color:var(--bg-muted)] text-[10px] text-[color:var(--text-muted)] uppercase"
              >
                <tr>
                  <th class="px-4 py-2 font-medium">Step</th>
                  <th class="px-3 py-2 font-medium">Sent</th>
                  <th class="px-3 py-2 font-medium">A / B</th>
                  <th class="px-3 py-2 font-medium">Open</th>
                  <th class="px-3 py-2 font-medium">Click</th>
                  <th class="px-3 py-2 font-medium">Reply</th>
                  <th class="px-3 py-2 font-medium">Bounce</th>
                </tr>
              </thead>
              <tbody class="divide-y divide-[color:var(--border-faint)]">
                {#each campaignAnalytics.steps || [] as step (step.position)}
                  <tr>
                    <td class="px-4 py-3">
                      <p class="font-medium">Step {step.position}</p>
                      <p
                        class="mt-0.5 max-w-56 truncate text-[10px] text-[color:var(--text-muted)]"
                      >
                        {step.subject_a || 'Historical delivery step'}
                      </p>
                    </td>
                    <td class="px-3 py-3 font-medium tabular-nums">{step.sent || 0}</td>
                    <td class="px-3 py-3 tabular-nums">
                      {step.variants?.[0]?.sent || 0} / {step.variants?.[1]?.sent || 0}
                    </td>
                    <td class="px-3 py-3 tabular-nums">{formatPercent(step.open_rate)}</td>
                    <td class="px-3 py-3 tabular-nums">{formatPercent(step.click_rate)}</td>
                    <td class="px-3 py-3 tabular-nums">{formatPercent(step.reply_rate)}</td>
                    <td class="px-3 py-3 tabular-nums">{formatPercent(step.bounce_rate)}</td>
                  </tr>
                {:else}
                  <tr>
                    <td
                      colspan="7"
                      class="px-4 py-8 text-center text-xs text-[color:var(--text-muted)]"
                    >
                      No configured or historical email steps yet.
                    </td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
        </div>

        <div class="grid gap-4 sm:grid-cols-2 xl:grid-cols-1">
          <div class="rounded-lg border border-[color:var(--border-faint)] p-4">
            <div class="flex items-center gap-2">
              <Mail class="size-3.5 text-violet-600" />
              <h3 class="text-xs font-semibold">Email A/B summary</h3>
            </div>
            <div class="mt-3 grid grid-cols-2 gap-3">
              {#each emailMetrics.variants || [] as variant (variant.variant)}
                <div class="rounded-md bg-[color:var(--bg-muted)] p-3">
                  <p class="text-[10px] font-semibold text-violet-700">Variant {variant.variant}</p>
                  <p class="mt-1 text-lg font-semibold tabular-nums">{variant.sent || 0}</p>
                  <p class="mt-1 text-[10px] text-[color:var(--text-muted)]">
                    {formatPercent(variant.open_rate)} open · {formatPercent(variant.reply_rate)} reply
                  </p>
                </div>
              {/each}
            </div>
          </div>

          <div class="rounded-lg border border-[color:var(--border-faint)] p-4">
            <div class="flex items-center gap-2">
              <Linkedin class="size-3.5 text-blue-700" />
              <h3 class="text-xs font-semibold">LinkedIn invitations</h3>
            </div>
            <div class="mt-3 grid grid-cols-4 gap-2 text-center">
              {#each [{ label: 'Queued', value: linkedinMetrics.queued || 0 }, { label: 'Sent', value: linkedinMetrics.sent || 0 }, { label: 'Failed', value: linkedinMetrics.failed || 0 }, { label: 'Skipped', value: linkedinMetrics.skipped || 0 }] as metric (metric.label)}
                <div>
                  <p class="text-base font-semibold tabular-nums">{metric.value}</p>
                  <p class="text-[9px] text-[color:var(--text-muted)]">{metric.label}</p>
                </div>
              {/each}
            </div>
            <p class="mt-3 text-[10px] text-[color:var(--text-muted)]">
              Sent means LinkedIn accepted the invitation and returned its invitation ID.
            </p>
          </div>

          <div class="rounded-lg border border-[color:var(--border-faint)] p-4">
            <div class="flex items-center gap-2">
              <MessageCircle class="size-3.5 text-emerald-600" />
              <h3 class="text-xs font-semibold">WhatsApp</h3>
            </div>
            <div class="mt-3 grid grid-cols-4 gap-2 text-center">
              {#each [{ label: 'Sent', value: whatsappMetrics.sent || 0 }, { label: 'Delivered', value: whatsappMetrics.delivered || 0 }, { label: 'Read', value: whatsappMetrics.read || 0 }, { label: 'Failed', value: whatsappMetrics.failed || 0 }] as metric (metric.label)}
                <div>
                  <p class="text-base font-semibold tabular-nums">{metric.value}</p>
                  <p class="text-[9px] text-[color:var(--text-muted)]">{metric.label}</p>
                </div>
              {/each}
            </div>
            <p class="mt-3 text-[10px] text-[color:var(--text-muted)]">
              {formatPercent(whatsappMetrics.delivery_rate)} delivered ·
              {formatPercent(whatsappMetrics.read_rate)} read
            </p>
          </div>
        </div>
      </div>

      <div class="mt-5 grid gap-4 xl:grid-cols-2">
        {#each [{ title: 'Industry ICP', rows: campaignAnalytics.icp?.industries || [] }, { title: 'Country ICP', rows: campaignAnalytics.icp?.countries || [] }] as segment (segment.title)}
          <div class="overflow-hidden rounded-lg border border-[color:var(--border-faint)]">
            <div class="border-b border-[color:var(--border-faint)] px-4 py-3">
              <h3 class="text-xs font-semibold">{segment.title}</h3>
            </div>
            <div class="overflow-x-auto">
              <table class="w-full min-w-[520px] text-left text-xs">
                <thead
                  class="bg-[color:var(--bg-muted)] text-[10px] text-[color:var(--text-muted)] uppercase"
                >
                  <tr>
                    <th class="px-4 py-2 font-medium">Segment</th>
                    <th class="px-3 py-2 font-medium">Prospects</th>
                    <th class="px-3 py-2 font-medium">Sent</th>
                    <th class="px-3 py-2 font-medium">Reply</th>
                    <th class="px-3 py-2 font-medium">MQL</th>
                    <th class="px-3 py-2 font-medium">SQL</th>
                  </tr>
                </thead>
                <tbody class="divide-y divide-[color:var(--border-faint)]">
                  {#each segment.rows as row (row.value)}
                    <tr>
                      <td class="px-4 py-2.5 font-medium">{row.value}</td>
                      <td class="px-3 py-2.5 tabular-nums">{row.prospects}</td>
                      <td class="px-3 py-2.5 tabular-nums">{row.sent}</td>
                      <td class="px-3 py-2.5 tabular-nums">
                        {row.replied}
                        <span class="text-[10px] text-[color:var(--text-muted)]"
                          >({formatPercent(row.reply_rate)})</span
                        >
                      </td>
                      <td class="px-3 py-2.5 tabular-nums">{row.mql}</td>
                      <td class="px-3 py-2.5 tabular-nums">{row.sql}</td>
                    </tr>
                  {:else}
                    <tr>
                      <td colspan="6" class="px-4 py-6 text-center text-[color:var(--text-muted)]">
                        No ICP segment data yet.
                      </td>
                    </tr>
                  {/each}
                </tbody>
              </table>
            </div>
          </div>
        {/each}
      </div>
    </section>
  {/if}

  {#if selected && !creating}
    <section
      class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-5"
    >
      <div class="flex flex-wrap items-start justify-between gap-4">
        <div>
          <div class="flex items-center gap-2">
            <Search class="size-4 text-violet-600" />
            <h2 class="text-sm font-semibold">Automatic prospect sources</h2>
          </div>
          <p class="mt-1 text-xs text-[color:var(--text-muted)]">
            Search one Apollo page per run, skip Apollo IDs already in this campaign, then enrich
            and import only the configured maximum.
          </p>
        </div>
        <span class="rounded-full bg-violet-50 px-2.5 py-1 text-[11px] font-medium text-violet-700">
          {outboundSources.length} configured
        </span>
      </div>

      {#if outboundSources.length}
        <div class="mt-4 grid gap-3 lg:grid-cols-2">
          {#each outboundSources as source (source.id)}
            <div class="rounded-lg border border-[color:var(--border-faint)] p-4">
              <div class="flex items-start justify-between gap-3">
                <div>
                  <div class="flex items-center gap-2">
                    <p class="text-sm font-medium">{source.name}</p>
                    <span
                      class={`rounded-full px-2 py-0.5 text-[10px] font-medium ${source.is_active ? 'bg-emerald-100 text-emerald-700' : 'bg-slate-100 text-slate-600'}`}
                    >
                      {source.is_active ? 'Scheduled' : 'Manual'}
                    </span>
                  </div>
                  <p class="mt-1 text-[11px] text-[color:var(--text-muted)]">
                    Up to {source.max_results_per_sync} enrichments every {source.interval_hours}h ·
                    page {source.next_page}
                  </p>
                </div>
                <form method="POST" action="?/syncApolloSource" use:enhance>
                  <input type="hidden" name="source_id" value={source.id} />
                  <Button size="sm" variant="outline" type="submit" disabled={!apolloActive}>
                    <RefreshCw class="size-3.5" />Sync now
                  </Button>
                </form>
              </div>
              {#if source.last_sync_at}
                <p class="mt-3 text-[11px] text-[color:var(--text-muted)]">
                  Last sync {formatDate(source.last_sync_at)} · {source.last_sync_stats?.created ||
                    0}
                  created · {source.last_sync_stats?.duplicates || 0} duplicates ·
                  {source.last_sync_stats?.enrichment_requests || 0} credits requested
                </p>
              {/if}
              {#if source.last_error_message}
                <p class="mt-3 rounded-md bg-red-50 px-3 py-2 text-[11px] text-red-700">
                  {source.last_error_message}
                </p>
              {/if}
            </div>
          {/each}
        </div>
      {/if}

      <form method="POST" action="?/saveApolloSource" use:enhance class="mt-5 space-y-4">
        <input type="hidden" name="campaign_id" value={selected.id} />
        <div class="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
          <label class="space-y-1.5 text-xs font-medium">
            Source name
            <input
              name="name"
              required
              maxlength="160"
              placeholder="DACH operations leaders"
              class="h-9 w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg)] px-3 text-sm font-normal"
            />
          </label>
          <label class="space-y-1.5 text-xs font-medium">
            Job titles <span class="font-normal text-[color:var(--text-muted)]"
              >comma-separated</span
            >
            <input
              name="person_titles"
              placeholder="VP Operations, COO"
              class="h-9 w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg)] px-3 text-sm font-normal"
            />
          </label>
          <label class="space-y-1.5 text-xs font-medium">
            Seniorities
            <input
              name="person_seniorities"
              placeholder="vp, c_suite"
              class="h-9 w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg)] px-3 text-sm font-normal"
            />
          </label>
          <label class="space-y-1.5 text-xs font-medium">
            Person locations
            <input
              name="person_locations"
              placeholder="Germany, Austria"
              class="h-9 w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg)] px-3 text-sm font-normal"
            />
          </label>
          <label class="space-y-1.5 text-xs font-medium">
            Company locations
            <input
              name="organization_locations"
              placeholder="Germany"
              class="h-9 w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg)] px-3 text-sm font-normal"
            />
          </label>
          <label class="space-y-1.5 text-xs font-medium">
            Company domains
            <input
              name="organization_domains"
              placeholder="example.com, factory.de"
              class="h-9 w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg)] px-3 text-sm font-normal"
            />
          </label>
          <label class="space-y-1.5 text-xs font-medium">
            Keywords
            <input
              name="keywords"
              maxlength="200"
              placeholder="industrial automation"
              class="h-9 w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg)] px-3 text-sm font-normal"
            />
          </label>
          <div class="grid grid-cols-2 gap-3">
            <label class="space-y-1.5 text-xs font-medium">
              Every (hours)
              <input
                name="interval_hours"
                type="number"
                min="1"
                max="168"
                value="24"
                class="h-9 w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg)] px-3 text-sm font-normal"
              />
            </label>
            <label class="space-y-1.5 text-xs font-medium">
              Max credits/run
              <input
                name="max_results_per_sync"
                type="number"
                min="1"
                max="100"
                value="25"
                class="h-9 w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg)] px-3 text-sm font-normal"
              />
            </label>
          </div>
        </div>
        <div class="flex flex-wrap items-center justify-between gap-3">
          <div class="space-y-2">
            <label class="flex items-center gap-2 text-xs">
              <input
                name="enrichment_credits_acknowledged"
                type="checkbox"
                required
                class="size-4 rounded border-[color:var(--border-faint)]"
              />
              I understand that Apollo person enrichment consumes account credits.
            </label>
            <label class="flex items-center gap-2 text-xs text-[color:var(--text-muted)]">
              <input
                name="is_active"
                type="checkbox"
                disabled={!apolloActive}
                class="size-4 rounded border-[color:var(--border-faint)]"
              />
              Run automatically every interval
            </label>
          </div>
          <Button size="sm" type="submit">Add Apollo source</Button>
        </div>
        {#if !apolloActive}
          <p class="rounded-md bg-amber-50 px-3 py-2 text-xs text-amber-800">
            Save and enable the Apollo connection above before scheduling or manually syncing a
            source.
          </p>
        {/if}
      </form>
    </section>

    <section
      class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-5"
    >
      <div class="flex flex-wrap items-start justify-between gap-4">
        <div>
          <div class="flex items-center gap-2">
            <FileSpreadsheet class="size-4 text-emerald-600" />
            <h2 class="text-sm font-semibold">Import prospect list</h2>
          </div>
          <p class="mt-1 text-xs text-[color:var(--text-muted)]">
            Up to 500 rows. Duplicates are checked within the file, every campaign, and existing CRM
            leads. Add a reviewed lawful basis before enabling compliance enforcement.
          </p>
        </div>
        <code
          class="rounded bg-[color:var(--bg-muted)] px-2 py-1 text-[10px] text-[color:var(--text-muted)]"
        >
          company_name,email,country,lawful_basis,allowed_channels
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
          placeholder={prospectCsvPlaceholder}></textarea>
        <div class="mt-3 flex flex-wrap items-center justify-between gap-3">
          <label class="flex items-center gap-2 text-xs text-[color:var(--text-muted)]">
            <input
              name="promote_ready"
              type="checkbox"
              class="size-4 rounded border-[color:var(--border-faint)]"
            />
            Queue every valid new prospect into the SDR pipeline immediately
          </label>
          <Button size="sm" type="submit"><Upload class="size-3.5" />Import CSV</Button>
        </div>
      </form>

      {#if form?.importResult}
        <div class="mt-5 grid gap-3 sm:grid-cols-4">
          {#each [{ label: 'Created', value: form.importResult.created, tone: 'text-emerald-700' }, { label: 'Queued', value: form.importResult.queued, tone: 'text-blue-700' }, { label: 'Duplicates', value: form.importResult.duplicate_count, tone: 'text-amber-700' }, { label: 'Errors', value: form.importResult.error_count, tone: 'text-red-700' }] as item (item.label)}
            <div class="rounded-lg bg-[color:var(--bg-muted)] p-3">
              <p class="text-[11px] text-[color:var(--text-muted)]">{item.label}</p>
              <p class={`mt-1 text-lg font-semibold tabular-nums ${item.tone}`}>
                {item.value || 0}
              </p>
            </div>
          {/each}
        </div>
        {#if form.importResult.errors?.length || form.importResult.duplicates?.length}
          <div
            class="mt-3 max-h-36 overflow-y-auto rounded-lg border border-[color:var(--border-faint)] p-3 text-[11px] text-[color:var(--text-muted)]"
          >
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

    <section
      class="overflow-hidden rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)]"
    >
      <div
        class="flex flex-wrap items-center justify-between gap-3 border-b border-[color:var(--border-faint)] p-5"
      >
        <div>
          <h2 class="text-sm font-semibold">Prospects · {selected.name}</h2>
          <p class="mt-1 text-xs text-[color:var(--text-muted)]">
            {data.prospects?.count || 0} unique records · {prospectSummary.ready || 0} ready · {prospectSummary.promoted ||
              0} promoted
          </p>
        </div>
        <span
          class={`rounded-full px-2.5 py-1 text-[11px] font-medium ${statusClass(selected.status)}`}
        >
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
                    <p class="mt-0.5 text-[11px] text-[color:var(--text-muted)]">
                      {prospect.job_title || prospect.email || '-'}
                    </p>
                  </td>
                  <td class="px-4 py-3">
                    <div class="flex items-center gap-2">
                      <Building2 class="size-3.5 text-[color:var(--text-subtle)]" />
                      <span>{prospect.company_name}</span>
                    </div>
                  </td>
                  <td class="px-4 py-3 text-[color:var(--text-muted)]">{prospect.country || '-'}</td
                  >
                  <td class="px-4 py-3">
                    <span
                      class={`rounded-full px-2 py-0.5 text-[10px] font-medium ${statusClass(prospect.status)}`}
                    >
                      {prospect.status}
                    </span>
                    {#if prospect.last_error_message}
                      <p
                        class="mt-1 max-w-48 truncate text-[10px] text-red-600"
                        title={prospect.last_error_message}
                      >
                        {prospect.last_error_message}
                      </p>
                    {/if}
                  </td>
                  <td class="px-4 py-3 text-[color:var(--text-muted)]"
                    >{formatDate(prospect.created_at)}</td
                  >
                  <td class="px-5 py-3">
                    <div class="flex justify-end gap-1.5">
                      {#if prospect.status === 'ready' || prospect.status === 'failed'}
                        <form method="POST" action="?/prospectAction" use:enhance>
                          <input type="hidden" name="prospect_id" value={prospect.id} />
                          <input type="hidden" name="prospect_action" value="promote" />
                          <Button size="sm" variant="outline" type="submit">
                            {#if prospect.status === 'failed'}<RefreshCw
                                class="size-3"
                              />{:else}<Rocket class="size-3" />{/if}
                            {prospect.status === 'failed' ? 'Retry' : 'Promote'}
                          </Button>
                        </form>
                        <form method="POST" action="?/prospectAction" use:enhance>
                          <input type="hidden" name="prospect_id" value={prospect.id} />
                          <input type="hidden" name="prospect_action" value="disqualify" />
                          <Button size="sm" variant="ghost" type="submit"
                            ><Ban class="size-3" />Skip</Button
                          >
                        </form>
                      {:else if prospect.status === 'disqualified'}
                        <form method="POST" action="?/prospectAction" use:enhance>
                          <input type="hidden" name="prospect_id" value={prospect.id} />
                          <input type="hidden" name="prospect_action" value="restore" />
                          <Button size="sm" variant="outline" type="submit"
                            ><RefreshCw class="size-3" />Restore</Button
                          >
                        </form>
                      {:else if prospect.status === 'promoted' && prospect.lead_id}
                        <a
                          href={`/leads/${prospect.lead_id}`}
                          class="inline-flex items-center gap-1 text-xs font-medium text-violet-600 hover:underline"
                        >
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
          <p class="mt-1 text-xs text-[color:var(--text-muted)]">
            Paste a researched CSV list above to begin cleaning and deduplication.
          </p>
        </div>
      {/if}
    </section>
  {/if}
</div>
