<script>
  import { enhance } from '$app/forms';
  import { toast } from 'svelte-sonner';
  import { Button } from '$lib/components/ui/button/index.js';
  import { Badge } from '$lib/components/ui/badge/index.js';
  import { Input } from '$lib/components/ui/input/index.js';
  import { SectionCard } from '$lib/components/ui/section-card/index.js';
  import { PageHeader } from '$lib/components/layout';
  import {
    AlertTriangle,
    CheckCircle2,
    Clock3,
    DatabaseZap,
    LoaderCircle,
    MailCheck,
    MessageCircle,
    RefreshCw,
    RotateCcw,
    Timer
  } from '@lucide/svelte';

  /** @type {{ data: any, form: any }} */
  let { data, form } = $props();

  let retryingId = $state('');
  const summary = $derived(data.jobs?.summary || {});
  const intakes = $derived(data.intakes?.results || []);
  const intakeSummary = $derived(data.intakes?.summary || {});
  const responseMetrics = $derived(data.intakes?.response_metrics || {});
  const responseSettings = $derived(data.responseSettings || {});
  const feishuBase = $derived(data.feishuBase || {});
  const feishuSyncSummary = $derived(feishuBase.sync_summary || {});
  const feishuMappingFields = [
    { key: 'intake_id', label: 'Intake ID', required: true, hint: 'Unique text business key' },
    { key: 'company_name', label: 'Company', hint: 'Text' },
    { key: 'contact_name', label: 'Contact', hint: 'Text' },
    { key: 'email', label: 'Email', hint: 'Text' },
    { key: 'phone', label: 'Phone', hint: 'Text or phone' },
    { key: 'linkedin_url', label: 'LinkedIn URL', hint: 'Text or URL' },
    { key: 'website', label: 'Website', hint: 'Text or URL' },
    { key: 'source', label: 'Source', hint: 'Text or single select' },
    { key: 'source_record_id', label: 'Source record ID', hint: 'Text' },
    { key: 'research_summary', label: 'Research summary', hint: 'Text' },
    { key: 'research_facts', label: 'Research facts', hint: 'Text (JSON)' },
    { key: 'source_urls', label: 'Source URLs', hint: 'Text (JSON)' },
    { key: 'qualification_score', label: 'Qualification score', hint: 'Number' },
    { key: 'qualification_band', label: 'Qualification band', hint: 'Text or single select' },
    { key: 'qualification_reasons', label: 'Qualification reasons', hint: 'Text (JSON)' },
    { key: 'assigned_sales', label: 'Assigned sales', hint: 'Text' },
    { key: 'routing_reason', label: 'Routing reason', hint: 'Text' },
    { key: 'crm_lead_id', label: 'CRM lead ID', hint: 'Text' },
    { key: 'processed_at', label: 'Processed at', hint: 'Date/time' },
    { key: 'inspection_status', label: 'Inspection status', hint: 'Text or single select' }
  ];
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
    if (form?.responseSaved) toast.success('Lead response settings saved.');
    if (form?.feishuBaseSaved) toast.success('Feishu Base connection saved.');
    if (form?.feishuBaseTested) {
      toast.success(`Feishu Base mapping validated against ${form.feishuBaseFieldCount} fields.`);
    }
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
    return (
      {
        'facebook.process_lead': 'Facebook lead intake',
        'sdr.process_intake': 'Website lead intake',
        'sdr.send_acknowledgement': 'Acknowledgement email',
        'sdr.notify_sales_in_app': 'Sales in-app notification',
        'sdr.notify_sales_feishu': 'Sales Feishu notification',
        'feishu_base.sync_research_result': 'Feishu Base research sync'
      }[name] || name
    );
  }

  /** @param {string} status */
  function intakeStatusClass(status) {
    if (status === 'completed') return 'border-emerald-200 bg-emerald-50 text-emerald-700';
    if (status === 'failed') return 'border-red-200 bg-red-50 text-red-700';
    if (status === 'processing') return 'border-blue-200 bg-blue-50 text-blue-700';
    return 'border-amber-200 bg-amber-50 text-amber-700';
  }

  /** @param {any} intake @param {string} kind */
  function deliveryStatus(intake, kind) {
    return intake.deliveries?.find((delivery) => delivery.kind === kind)?.status || 'not_scheduled';
  }

  /** @param {number | null | undefined} seconds */
  function formatDuration(seconds) {
    if (seconds === null || seconds === undefined) return '—';
    if (seconds < 60) return `${seconds}s`;
    return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
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

  <section class="space-y-3">
    <div>
      <h2 class="text-base font-medium text-[color:var(--text-primary)]">Lead response health</h2>
      <p class="text-xs text-[color:var(--text-muted)]">
        Customer acknowledgement, sales handoff, and the response SLA for recent inbound leads.
      </p>
    </div>
    <div class="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
      <div
        class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-4"
      >
        <div class="flex items-center justify-between">
          <p class="text-xs font-medium text-[color:var(--text-muted)]">Inbound leads</p>
          <MessageCircle class="size-4 text-violet-500" />
        </div>
        <p class="mt-2 text-2xl font-semibold text-[color:var(--text-primary)]">
          {data.intakes?.count || 0}
        </p>
        <p class="mt-1 text-xs text-[color:var(--text-muted)]">
          {intakeSummary.completed || 0} completed
        </p>
      </div>
      <div
        class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-4"
      >
        <div class="flex items-center justify-between">
          <p class="text-xs font-medium text-[color:var(--text-muted)]">Acknowledged</p>
          <MailCheck class="size-4 text-emerald-500" />
        </div>
        <p class="mt-2 text-2xl font-semibold text-[color:var(--text-primary)]">
          {responseMetrics.responded || 0}
        </p>
        <p class="mt-1 text-xs text-[color:var(--text-muted)]">Recent sample of {responseMetrics.sample_size || 0}</p>
      </div>
      <div
        class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-4"
      >
        <div class="flex items-center justify-between">
          <p class="text-xs font-medium text-[color:var(--text-muted)]">Average response</p>
          <Timer class="size-4 text-blue-500" />
        </div>
        <p class="mt-2 text-2xl font-semibold text-[color:var(--text-primary)]">
          {formatDuration(responseMetrics.average_response_seconds)}
        </p>
        <p class="mt-1 text-xs text-[color:var(--text-muted)]">
          SLA {formatDuration(responseMetrics.sla_seconds)}
        </p>
      </div>
      <div
        class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-4"
      >
        <div class="flex items-center justify-between">
          <p class="text-xs font-medium text-[color:var(--text-muted)]">SLA breached</p>
          <AlertTriangle class="size-4 text-red-500" />
        </div>
        <p class="mt-2 text-2xl font-semibold text-[color:var(--text-primary)]">
          {responseMetrics.sla_breached || 0}
        </p>
        <p class="mt-1 text-xs text-[color:var(--text-muted)]">Needs response review</p>
      </div>
    </div>
  </section>

  <SectionCard>
    {#snippet title()}
      <div>
        <h3 class="text-[16px] font-medium text-[color:var(--text-primary)]">Response policy</h3>
        <p class="text-[12px] text-[color:var(--text-muted)]">
          Configure immediate customer acknowledgement and sales notification channels.
        </p>
      </div>
    {/snippet}

    <form method="POST" action="?/saveResponse" use:enhance class="space-y-5">
      <div class="grid gap-3 md:grid-cols-3">
        <label
          class="flex items-start gap-2 rounded-md border border-[color:var(--border-faint)] p-3 text-sm"
        >
          <input
            type="checkbox"
            name="acknowledgement_email_enabled"
            checked={responseSettings.acknowledgement_email_enabled}
            class="mt-0.5"
          />
          <span>
            <strong class="block">Customer email</strong>
            <span class="text-xs text-[color:var(--text-muted)]">Acknowledge each valid email once.</span>
          </span>
        </label>
        <label
          class="flex items-start gap-2 rounded-md border border-[color:var(--border-faint)] p-3 text-sm"
        >
          <input
            type="checkbox"
            name="sales_in_app_enabled"
            checked={responseSettings.sales_in_app_enabled}
            class="mt-0.5"
          />
          <span>
            <strong class="block">In-app sales alert</strong>
            <span class="text-xs text-[color:var(--text-muted)]">Notify the assigned CRM user.</span>
          </span>
        </label>
        <label
          class="flex items-start gap-2 rounded-md border border-[color:var(--border-faint)] p-3 text-sm"
        >
          <input
            type="checkbox"
            name="feishu_enabled"
            checked={responseSettings.feishu_enabled}
            class="mt-0.5"
          />
          <span>
            <strong class="block">Feishu group alert</strong>
            <span class="text-xs text-[color:var(--text-muted)]">Send the complete handoff to a custom bot.</span>
          </span>
        </label>
      </div>

      <div class="grid gap-4 md:grid-cols-2">
        <label class="space-y-1.5 text-sm">
          <span class="font-medium text-[color:var(--text-primary)]">Email subject</span>
          <Input
            name="acknowledgement_subject"
            value={responseSettings.acknowledgement_subject || ''}
            maxlength="255"
            required
          />
        </label>
        <label class="space-y-1.5 text-sm">
          <span class="font-medium text-[color:var(--text-primary)]">From email</span>
          <Input
            type="email"
            name="acknowledgement_from_email"
            value={responseSettings.acknowledgement_from_email || ''}
            placeholder="Use platform default"
          />
        </label>
      </div>

      <label class="block space-y-1.5 text-sm">
        <span class="font-medium text-[color:var(--text-primary)]">Email body</span>
        <textarea
          name="acknowledgement_body"
          rows="6"
          required
          class="w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] px-3 py-2 text-sm text-[color:var(--text-primary)]"
        >{responseSettings.acknowledgement_body || ''}</textarea>
        <span class="text-xs text-[color:var(--text-muted)]">
          Variables: {'{{ first_name }}'}, {'{{ company_name }}'}, {'{{ organization_name }}'}.
        </span>
      </label>

      <div class="grid gap-4 md:grid-cols-[1fr_180px]">
        <label class="space-y-1.5 text-sm">
          <span class="font-medium text-[color:var(--text-primary)]">Feishu custom-bot webhook</span>
          <Input
            type="password"
            name="feishu_webhook_url"
            autocomplete="new-password"
            placeholder={responseSettings.feishu_configured
              ? `Configured …${responseSettings.feishu_webhook_hint}`
              : 'https://open.feishu.cn/open-apis/bot/v2/hook/...'}
          />
        </label>
        <label class="space-y-1.5 text-sm">
          <span class="font-medium text-[color:var(--text-primary)]">Response SLA (seconds)</span>
          <Input
            type="number"
            name="response_sla_seconds"
            min="1"
            max="86400"
            value={responseSettings.response_sla_seconds || 60}
            required
          />
        </label>
      </div>

      <div class="flex flex-wrap items-center justify-between gap-3">
        <label class="flex items-center gap-2 text-xs text-[color:var(--text-muted)]">
          <input type="checkbox" name="clear_feishu_webhook" />
          Clear the stored Feishu webhook
        </label>
        <Button type="submit">Save response policy</Button>
      </div>
    </form>
  </SectionCard>

  <SectionCard>
    {#snippet title()}
      <div>
        <h3 class="flex items-center gap-2 text-[16px] font-medium text-[color:var(--text-primary)]">
          <DatabaseZap class="size-4 text-violet-500" />Feishu Base research sync
        </h3>
        <p class="text-[12px] text-[color:var(--text-muted)]">
          Upsert each completed research result by intake ID through the official Base API.
        </p>
      </div>
    {/snippet}

    <div class="mb-5 grid gap-3 sm:grid-cols-3">
      <div class="rounded-md border border-[color:var(--border-faint)] p-3">
        <p class="text-xs text-[color:var(--text-muted)]">Synced</p>
        <p class="mt-1 text-xl font-semibold text-emerald-600">{feishuSyncSummary.succeeded || 0}</p>
      </div>
      <div class="rounded-md border border-[color:var(--border-faint)] p-3">
        <p class="text-xs text-[color:var(--text-muted)]">In progress</p>
        <p class="mt-1 text-xl font-semibold text-blue-600">
          {(feishuSyncSummary.pending || 0) + (feishuSyncSummary.queued || 0) + (feishuSyncSummary.syncing || 0)}
        </p>
      </div>
      <div class="rounded-md border border-[color:var(--border-faint)] p-3">
        <p class="text-xs text-[color:var(--text-muted)]">Failed</p>
        <p class="mt-1 text-xl font-semibold text-red-600">{feishuSyncSummary.failed || 0}</p>
      </div>
    </div>

    <form method="POST" action="?/saveFeishuBase" use:enhance class="space-y-5">
      <label class="flex items-start gap-2 rounded-md border border-[color:var(--border-faint)] p-3 text-sm">
        <input type="checkbox" name="is_active" checked={feishuBase.is_active} class="mt-0.5" />
        <span>
          <strong class="block">Enable automatic Base sync</strong>
          <span class="text-xs text-[color:var(--text-muted)]">
            New and recently completed intakes are queued through the durable job ledger.
          </span>
        </span>
      </label>

      <div class="grid gap-4 md:grid-cols-2">
        <label class="space-y-1.5 text-sm">
          <span class="font-medium text-[color:var(--text-primary)]">Feishu app ID</span>
          <Input name="app_id" value={feishuBase.app_id || ''} placeholder="cli_..." />
        </label>
        <label class="space-y-1.5 text-sm">
          <span class="font-medium text-[color:var(--text-primary)]">App secret</span>
          <Input
            type="password"
            name="app_secret"
            autocomplete="new-password"
            placeholder={feishuBase.app_secret_configured
              ? `Configured …${feishuBase.app_secret_hint}`
              : 'App secret'}
          />
        </label>
        <label class="space-y-1.5 text-sm">
          <span class="font-medium text-[color:var(--text-primary)]">Base app token</span>
          <Input name="app_token" value={feishuBase.app_token || ''} placeholder="bascn..." />
        </label>
        <label class="space-y-1.5 text-sm">
          <span class="font-medium text-[color:var(--text-primary)]">Table ID</span>
          <Input name="table_id" value={feishuBase.table_id || ''} placeholder="tbl..." />
        </label>
      </div>

      <div class="space-y-3">
        <div>
          <p class="text-sm font-medium text-[color:var(--text-primary)]">Field mapping</p>
          <p class="text-xs text-[color:var(--text-muted)]">
            Enter existing field names exactly as they appear in the target table. Formula, lookup,
            attachment, and system fields are not writable.
          </p>
        </div>
        <div class="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {#each feishuMappingFields as item (item.key)}
            <label class="space-y-1 text-xs">
              <span class="flex items-center justify-between gap-2">
                <span class="font-medium text-[color:var(--text-primary)]">
                  {item.label}{item.required ? ' *' : ''}
                </span>
                <span class="text-[color:var(--text-muted)]">{item.hint}</span>
              </span>
              <Input
                name={`mapping_${item.key}`}
                value={feishuBase.field_mapping?.[item.key] || ''}
                placeholder="Exact Base field name"
                required={item.required}
              />
            </label>
          {/each}
        </div>
      </div>

      <div class="flex flex-wrap items-center justify-between gap-3">
        <p class="max-w-2xl text-xs text-[color:var(--text-muted)]">
          Add the custom app to this Base with management permission and grant record/field read-write
          scopes. Credentials are encrypted at rest.
        </p>
        <Button type="submit">Save Base connection</Button>
      </div>
    </form>

    <div class="mt-4 flex flex-wrap items-center justify-between gap-3 border-t border-[color:var(--border-faint)] pt-4">
      <p class="text-xs text-[color:var(--text-muted)]">
        Last validated: {formatDate(feishuBase.last_validated_at)} · Last sync: {formatDate(feishuBase.last_sync_at)}
      </p>
      <form method="POST" action="?/testFeishuBase" use:enhance>
        <Button type="submit" variant="outline" disabled={!feishuBase.id}>Validate credentials & fields</Button>
      </form>
    </div>
  </SectionCard>

  <SectionCard>
    {#snippet title()}
      <div>
        <h3 class="text-[16px] font-medium text-[color:var(--text-primary)]">Lead response ledger</h3>
        <p class="text-[12px] text-[color:var(--text-muted)]">
          Intake, qualification, CRM handoff, and delivery state in one view.
        </p>
      </div>
    {/snippet}

    {#if intakes.length}
      <div class="overflow-x-auto rounded-lg border border-[color:var(--border-faint)]">
        <table class="w-full min-w-[1050px] text-left text-sm">
          <thead class="bg-[color:var(--bg-subtle)] text-xs text-[color:var(--text-muted)]">
            <tr>
              <th class="px-4 py-3 font-medium">Lead</th>
              <th class="px-4 py-3 font-medium">Intake</th>
              <th class="px-4 py-3 font-medium">Score / owner</th>
              <th class="px-4 py-3 font-medium">Acknowledgement</th>
              <th class="px-4 py-3 font-medium">Sales notification</th>
              <th class="px-4 py-3 font-medium">Received</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-[color:var(--border-faint)]">
            {#each intakes as intake (intake.id)}
              <tr class="bg-[color:var(--bg-elevated)] align-top">
                <td class="px-4 py-3">
                  {#if intake.lead_id}
                    <a
                      href="/leads/{intake.lead_id}"
                      class="font-medium text-violet-600 hover:underline"
                    >{intake.company_name || intake.contact_email || 'Inbound lead'}</a>
                  {:else}
                    <p class="font-medium text-[color:var(--text-primary)]">
                      {intake.company_name || intake.contact_email || 'Inbound lead'}
                    </p>
                  {/if}
                  <p class="mt-1 text-xs text-[color:var(--text-muted)]">{intake.contact_email || '—'}</p>
                </td>
                <td class="px-4 py-3">
                  <Badge class={intakeStatusClass(intake.status)}>{intake.status}</Badge>
                  <p class="mt-1 text-xs text-[color:var(--text-muted)]">{intake.source}</p>
                </td>
                <td class="px-4 py-3">
                  <p class="font-medium text-[color:var(--text-primary)]">
                    {intake.qualification_score ?? '—'} {intake.qualification_band || ''}
                  </p>
                  <p class="mt-1 text-xs text-[color:var(--text-muted)]">
                    {intake.assigned_sales?.name || intake.assigned_sales?.email || 'Unassigned'}
                  </p>
                </td>
                <td class="px-4 py-3">
                  <p class={intake.sla_breached ? 'font-medium text-red-600' : 'text-[color:var(--text-primary)]'}>
                    {deliveryStatus(intake, 'acknowledgement_email')}
                  </p>
                  <p class="mt-1 text-xs text-[color:var(--text-muted)]">
                    {formatDuration(intake.response_seconds)}
                  </p>
                </td>
                <td class="px-4 py-3 text-xs text-[color:var(--text-muted)]">
                  <p>In app: {deliveryStatus(intake, 'sales_in_app')}</p>
                  <p class="mt-1">Feishu: {deliveryStatus(intake, 'sales_feishu')}</p>
                </td>
                <td class="px-4 py-3 text-xs text-[color:var(--text-muted)]">
                  {formatDate(intake.created_at)}
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {:else}
      <div class="rounded-lg border border-dashed border-[color:var(--border)] p-8 text-center">
        <p class="text-sm font-medium text-[color:var(--text-primary)]">No inbound leads yet</p>
        <p class="mt-1 text-xs text-[color:var(--text-muted)]">
          Website and Facebook leads will appear after they are safely accepted.
        </p>
      </div>
    {/if}
  </SectionCard>

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
