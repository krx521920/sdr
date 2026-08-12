<script>
  import { enhance } from '$app/forms';
  import { toast } from 'svelte-sonner';
  import { Button } from '$lib/components/ui/button/index.js';
  import { SectionCard } from '$lib/components/ui/section-card/index.js';
  import { Badge } from '$lib/components/ui/badge/index.js';
  import { PageHeader } from '$lib/components/layout';
  import {
    CheckCircle2,
    Clock3,
    Link2,
    MessageCircle,
    Megaphone,
    RefreshCw,
    ShieldCheck,
    Trash2
  } from '@lucide/svelte';

  /** @type {{ data: any, form: any }} */
  let { data, form } = $props();

  let disconnectingId = $state('');
  let messengerUpdatingId = $state('');
  let messengerReplySavingId = $state('');

  $effect(() => {
    if (data.connected) toast.success('Facebook Page connected successfully.');
    if (data.oauthError) toast.error(oauthErrorMessage(data.oauthError));
    if (data.connectionError) toast.error(data.connectionError);
    if (form?.actionError) toast.error(form.actionError);
    if (form?.disconnected) toast.success('Facebook Page disconnected.');
    if (form?.messengerUpdated) {
      toast.success(
        form.messengerEnabled
          ? 'Messenger intake enabled for this Page.'
          : 'Messenger intake disabled for this Page.'
      );
    }
    if (form?.messengerReplySaved) {
      toast.success('Messenger auto-reply settings saved.');
    }
    if (form?.conversionSaved) {
      const backfilled = form.backfilledEvents || 0;
      toast.success(
        backfilled
          ? `Conversion feedback saved. ${backfilled} recent event${backfilled === 1 ? '' : 's'} queued.`
          : 'Conversion feedback settings saved.'
      );
    }
  });

  /** @param {string} value */
  function oauthErrorMessage(value) {
    if (value === 'authorization_denied') return 'Facebook authorization was cancelled.';
    if (value === 'invalid_state') return 'The authorization expired. Please try again.';
    if (value === 'provider_error') return 'Meta could not complete the authorization.';
    return 'Facebook authorization could not be completed.';
  }

  /** @param {string | null} value */
  function formatDate(value) {
    if (!value) return 'Not received yet';
    return new Intl.DateTimeFormat(undefined, {
      dateStyle: 'medium',
      timeStyle: 'short'
    }).format(new Date(value));
  }
</script>

<svelte:head>
  <title>Facebook Lead Ads - BottleCRM</title>
</svelte:head>

<PageHeader
  title="Facebook Lead Ads"
  subtitle="Authorize company Pages and route new Lead Ads submissions into your SDR workflow"
/>

<div class="mx-auto max-w-4xl space-y-6 p-6 md:p-8">
  {#if data.oauthSession?.status === 'ready'}
    <SectionCard>
      {#snippet title()}
        <div class="flex items-center gap-3">
          <div
            class="flex size-10 items-center justify-center rounded-lg bg-blue-100 dark:bg-blue-950"
          >
            <CheckCircle2 class="size-5 text-blue-600 dark:text-blue-400" />
          </div>
          <div>
            <h3 class="text-[16px] font-medium text-[color:var(--text-primary)]">
              Choose Pages to connect
            </h3>
            <p class="text-[12px] text-[color:var(--text-muted)]">
              Only selected Pages will send leads to this company workspace.
            </p>
          </div>
        </div>
      {/snippet}

      {#if data.oauthSession.pages?.length}
        <form method="POST" action="?/selectPages" class="space-y-4">
          <input type="hidden" name="session_id" value={data.oauthSession.id} />
          <div
            class="divide-y divide-[color:var(--border-faint)] overflow-hidden rounded-lg border border-[color:var(--border-faint)]"
          >
            {#each data.oauthSession.pages as facebookPage (facebookPage.id)}
              <label
                class="flex cursor-pointer items-center gap-3 bg-[color:var(--bg-elevated)] p-4 hover:bg-[color:var(--bg-subtle)]"
              >
                <input
                  type="checkbox"
                  name="page_ids"
                  value={facebookPage.id}
                  class="size-4 rounded border-[color:var(--border)] accent-blue-600"
                />
                <span class="flex min-w-0 flex-1 items-center gap-3">
                  <span
                    class="flex size-9 shrink-0 items-center justify-center rounded-full bg-blue-50 dark:bg-blue-950"
                  >
                    <Megaphone class="size-4 text-blue-600 dark:text-blue-400" />
                  </span>
                  <span class="min-w-0">
                    <span
                      class="block truncate text-sm font-medium text-[color:var(--text-primary)]"
                      >{facebookPage.name || 'Unnamed Page'}</span
                    >
                    <span class="block text-xs text-[color:var(--text-muted)]"
                      >Page ID: {facebookPage.id}</span
                    >
                  </span>
                </span>
              </label>
            {/each}
          </div>
          <div class="flex justify-end gap-2">
            <Button variant="outline" href="/settings/facebook">Cancel</Button>
            <Button type="submit" class="gap-2">
              <Link2 class="size-4" />
              Connect selected Pages
            </Button>
          </div>
        </form>
      {:else}
        <div class="rounded-lg border border-dashed border-[color:var(--border)] p-8 text-center">
          <p class="text-sm font-medium text-[color:var(--text-primary)]">
            No manageable Pages were returned
          </p>
          <p class="mt-1 text-xs text-[color:var(--text-muted)]">
            Confirm the Facebook account has Page access, then authorize again.
          </p>
          <form method="POST" action="?/startOAuth" class="mt-4">
            <Button type="submit" variant="outline" class="gap-2"
              ><RefreshCw class="size-4" />Authorize again</Button
            >
          </form>
        </div>
      {/if}
    </SectionCard>
  {:else}
    <SectionCard>
      {#snippet title()}
        <div class="flex items-center gap-3">
          <div
            class="flex size-10 items-center justify-center rounded-lg bg-blue-100 dark:bg-blue-950"
          >
            <ShieldCheck class="size-5 text-blue-600 dark:text-blue-400" />
          </div>
          <div>
            <h3 class="text-[16px] font-medium text-[color:var(--text-primary)]">
              Connect a Facebook account
            </h3>
            <p class="text-[12px] text-[color:var(--text-muted)]">
              A company administrator signs in to Meta and chooses the Pages this workspace may use.
            </p>
          </div>
        </div>
      {/snippet}
      <div class="grid gap-4 md:grid-cols-3">
        <div class="rounded-lg bg-[color:var(--bg-subtle)] p-4">
          <p class="text-sm font-medium text-[color:var(--text-primary)]">1. Authorize</p>
          <p class="mt-1 text-xs leading-5 text-[color:var(--text-muted)]">
            Sign in to Meta without sharing credentials with BottleCRM.
          </p>
        </div>
        <div class="rounded-lg bg-[color:var(--bg-subtle)] p-4">
          <p class="text-sm font-medium text-[color:var(--text-primary)]">2. Select Pages</p>
          <p class="mt-1 text-xs leading-5 text-[color:var(--text-muted)]">
            Choose which company Pages should be connected.
          </p>
        </div>
        <div class="rounded-lg bg-[color:var(--bg-subtle)] p-4">
          <p class="text-sm font-medium text-[color:var(--text-primary)]">3. Receive leads</p>
          <p class="mt-1 text-xs leading-5 text-[color:var(--text-muted)]">
            New Lead Ads submissions enter the shared SDR pipeline.
          </p>
        </div>
      </div>
      <form method="POST" action="?/startOAuth" class="mt-5">
        <Button type="submit" class="gap-2"><Link2 class="size-4" />Connect with Facebook</Button>
      </form>
    </SectionCard>
  {/if}

  <SectionCard>
    {#snippet title()}
      <div class="flex items-center gap-3">
        <Megaphone class="size-5 text-[color:var(--text-muted)]" />
        <div>
          <h3 class="text-[16px] font-medium text-[color:var(--text-primary)]">Connected Pages</h3>
          <p class="text-[12px] text-[color:var(--text-muted)]">
            Each Page is isolated to this company workspace.
          </p>
        </div>
      </div>
    {/snippet}
    {#snippet actions()}
      <Badge variant="outline">{data.connections.length} connected</Badge>
    {/snippet}

    {#if data.connections.length}
      <div class="space-y-3">
        {#each data.connections as connection (connection.id)}
          <div
            class="grid gap-4 rounded-lg border border-[color:var(--border-faint)] p-4 sm:grid-cols-[1fr_auto] sm:items-center"
          >
            <div class="flex min-w-0 flex-1 items-center gap-3">
              <div
                class="flex size-10 shrink-0 items-center justify-center rounded-full bg-blue-50 dark:bg-blue-950"
              >
                <Megaphone class="size-4 text-blue-600 dark:text-blue-400" />
              </div>
              <div class="min-w-0">
                <div class="flex items-center gap-2">
                  <p class="truncate text-sm font-medium text-[color:var(--text-primary)]">
                    {connection.page_name || 'Facebook Page'}
                  </p>
                  <Badge
                    class="border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-900 dark:bg-emerald-950 dark:text-emerald-400"
                    >Active</Badge
                  >
                </div>
                <p class="mt-1 text-xs text-[color:var(--text-muted)]">
                  Page {connection.page_id} · token ending {connection.access_token_hint}
                </p>
                <p class="mt-1 flex items-center gap-1 text-xs text-[color:var(--text-muted)]">
                  <Clock3 class="size-3" />Last lead webhook: {formatDate(
                    connection.last_webhook_at
                  )}
                </p>
              </div>
            </div>
            <form
              method="POST"
              action="?/disconnect"
              use:enhance={() => {
                disconnectingId = connection.id;
                return async ({ update }) => {
                  disconnectingId = '';
                  await update({ reset: false, invalidateAll: true });
                };
              }}
            >
              <input type="hidden" name="connection_id" value={connection.id} />
              <Button
                type="submit"
                variant="destructive"
                size="sm"
                disabled={disconnectingId === connection.id}
                class="gap-2"
              >
                <Trash2 class="size-3.5" />
                {disconnectingId === connection.id ? 'Disconnecting…' : 'Disconnect'}
              </Button>
            </form>
            <div
              class="flex flex-col gap-3 rounded-lg bg-[color:var(--bg-subtle)] p-3 sm:col-span-2 sm:flex-row sm:items-center"
            >
              <div class="flex min-w-0 flex-1 items-start gap-3">
                <MessageCircle class="mt-0.5 size-4 shrink-0 text-blue-600 dark:text-blue-400" />
                <div class="min-w-0">
                  <div class="flex flex-wrap items-center gap-2">
                    <p class="text-sm font-medium text-[color:var(--text-primary)]">
                      Messenger lead intake
                    </p>
                    <Badge
                      variant="outline"
                      class={connection.messenger_enabled
                        ? 'border-emerald-200 text-emerald-700 dark:border-emerald-900 dark:text-emerald-400'
                        : ''}
                    >
                      {connection.messenger_enabled ? 'Enabled' : 'Disabled'}
                    </Badge>
                  </div>
                  <p class="mt-1 text-xs leading-5 text-[color:var(--text-muted)]">
                    {connection.messenger_message_count || 0} messages · Last received:
                    {formatDate(connection.last_message_at)}
                  </p>
                  <p class="text-xs leading-5 text-[color:var(--text-muted)]">
                    Enabling requires the Meta pages_messaging permission. Disabling stops intake
                    without affecting Lead Ads.
                  </p>
                </div>
              </div>
              <form
                method="POST"
                action="?/toggleMessenger"
                use:enhance={() => {
                  messengerUpdatingId = connection.id;
                  return async ({ update }) => {
                    messengerUpdatingId = '';
                    await update({ reset: false, invalidateAll: true });
                  };
                }}
              >
                <input type="hidden" name="connection_id" value={connection.id} />
                <input
                  type="hidden"
                  name="messenger_enabled"
                  value={connection.messenger_enabled ? 'false' : 'true'}
                />
                <Button
                  type="submit"
                  variant={connection.messenger_enabled ? 'outline' : 'default'}
                  size="sm"
                  disabled={messengerUpdatingId === connection.id}
                  class="w-full gap-2 sm:w-auto"
                >
                  <MessageCircle class="size-3.5" />
                  {messengerUpdatingId === connection.id
                    ? 'Updating…'
                    : connection.messenger_enabled
                      ? 'Disable Messenger'
                      : 'Enable Messenger'}
                </Button>
              </form>
            </div>
            {#if connection.messenger_enabled}
              <form
                method="POST"
                action="?/saveMessengerReply"
                class="space-y-3 rounded-lg border border-[color:var(--border-faint)] p-3 sm:col-span-2"
                use:enhance={() => {
                  messengerReplySavingId = connection.id;
                  return async ({ update }) => {
                    messengerReplySavingId = '';
                    await update({ reset: false, invalidateAll: true });
                  };
                }}
              >
                <input type="hidden" name="connection_id" value={connection.id} />
                <label class="flex items-start gap-3">
                  <input
                    type="checkbox"
                    name="messenger_auto_reply_enabled"
                    checked={connection.messenger_auto_reply_enabled}
                    class="mt-0.5 size-4 rounded border-[color:var(--border)] accent-blue-600"
                  />
                  <span>
                    <span class="block text-sm font-medium text-[color:var(--text-primary)]">
                      Send an immediate first-response message
                    </span>
                    <span class="mt-1 block text-xs leading-5 text-[color:var(--text-muted)]">
                      Sent once per Page conversation inside Meta's 24-hour response window.
                    </span>
                  </span>
                </label>

                <label class="block space-y-1.5">
                  <span class="text-xs font-medium text-[color:var(--text-primary)]">
                    Auto-reply message
                  </span>
                  <textarea
                    name="messenger_auto_reply_template"
                    rows="3"
                    maxlength="2000"
                    required
                    class="w-full resize-y rounded-md border border-[color:var(--border)] bg-[color:var(--bg-elevated)] px-3 py-2 text-sm leading-5"
                    >{connection.messenger_auto_reply_template || ''}</textarea
                  >
                  <span class="block text-[11px] text-[color:var(--text-muted)]">
                    Variables: {'{{ page_name }}'} and {'{{ organization_name }}'}
                  </span>
                </label>

                <div class="grid gap-2 sm:grid-cols-4">
                  <div class="rounded-md bg-[color:var(--bg-subtle)] px-3 py-2">
                    <p class="text-[11px] text-[color:var(--text-muted)]">Sent</p>
                    <p class="text-sm font-semibold text-[color:var(--text-primary)]">
                      {connection.messenger_reply_summary?.sent || 0}
                    </p>
                  </div>
                  <div class="rounded-md bg-[color:var(--bg-subtle)] px-3 py-2">
                    <p class="text-[11px] text-[color:var(--text-muted)]">Pending</p>
                    <p class="text-sm font-semibold text-[color:var(--text-primary)]">
                      {(connection.messenger_reply_summary?.pending || 0) +
                        (connection.messenger_reply_summary?.queued || 0) +
                        (connection.messenger_reply_summary?.sending || 0)}
                    </p>
                  </div>
                  <div class="rounded-md bg-[color:var(--bg-subtle)] px-3 py-2">
                    <p class="text-[11px] text-[color:var(--text-muted)]">Failed</p>
                    <p class="text-sm font-semibold text-[color:var(--text-primary)]">
                      {connection.messenger_reply_summary?.failed || 0}
                    </p>
                  </div>
                  <div class="rounded-md bg-[color:var(--bg-subtle)] px-3 py-2">
                    <p class="text-[11px] text-[color:var(--text-muted)]">Last reply</p>
                    <p class="truncate text-xs font-medium text-[color:var(--text-primary)]">
                      {formatDate(connection.last_message_reply_at)}
                    </p>
                  </div>
                </div>

                <div class="flex justify-end">
                  <Button
                    type="submit"
                    size="sm"
                    disabled={messengerReplySavingId === connection.id}
                  >
                    {messengerReplySavingId === connection.id ? 'Saving…' : 'Save auto-reply'}
                  </Button>
                </div>
              </form>
            {/if}
          </div>
        {/each}
      </div>
    {:else}
      <div class="rounded-lg border border-dashed border-[color:var(--border)] p-8 text-center">
        <p class="text-sm font-medium text-[color:var(--text-primary)]">
          No Facebook Pages connected
        </p>
        <p class="mt-1 text-xs text-[color:var(--text-muted)]">
          Authorize a Meta account above to get started.
        </p>
      </div>
    {/if}
  </SectionCard>

  <SectionCard>
    {#snippet title()}
      <div class="flex items-center gap-3">
        <RefreshCw class="size-5 text-[color:var(--text-muted)]" />
        <div>
          <h3 class="text-[16px] font-medium text-[color:var(--text-primary)]">
            Conversion Leads feedback
          </h3>
          <p class="text-[12px] text-[color:var(--text-muted)]">
            Return CRM funnel stages to Meta so campaigns can optimize for lead quality.
          </p>
        </div>
      </div>
    {/snippet}
    {#snippet actions()}
      <Badge variant="outline">{data.conversion?.event_summary?.sent || 0} sent</Badge>
    {/snippet}

    <form method="POST" action="?/saveConversions" class="space-y-5">
      <label
        class="flex items-start gap-3 rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-subtle)] p-4"
      >
        <input
          type="checkbox"
          name="is_enabled"
          checked={data.conversion?.is_enabled}
          class="mt-0.5 size-4 rounded border-[color:var(--border)] accent-blue-600"
        />
        <span>
          <span class="block text-sm font-medium text-[color:var(--text-primary)]">
            Enable CRM conversion feedback
          </span>
          <span class="mt-1 block text-xs leading-5 text-[color:var(--text-muted)]">
            Only Facebook Lead Ads carrying a valid Meta Lead ID are eligible. Customer email and
            phone data are never included.
          </span>
        </span>
      </label>

      <div class="grid gap-4 md:grid-cols-2">
        <label class="space-y-1.5">
          <span class="text-xs font-medium text-[color:var(--text-primary)]">Meta Pixel ID</span>
          <input
            name="pixel_id"
            value={data.conversion?.pixel_id || ''}
            inputmode="numeric"
            placeholder="123456789012345"
            class="w-full rounded-md border border-[color:var(--border)] bg-[color:var(--bg-elevated)] px-3 py-2 text-sm"
          />
        </label>
        <label class="space-y-1.5">
          <span class="text-xs font-medium text-[color:var(--text-primary)]">
            Conversions API access token
          </span>
          <input
            name="access_token"
            type="password"
            autocomplete="new-password"
            placeholder={data.conversion?.access_token_configured
              ? `Token ending ${data.conversion.access_token_hint}`
              : 'Paste token from Events Manager'}
            class="w-full rounded-md border border-[color:var(--border)] bg-[color:var(--bg-elevated)] px-3 py-2 text-sm"
          />
          {#if data.conversion?.access_token_configured}
            <span class="block text-[11px] text-[color:var(--text-muted)]">
              Leave blank to retain the encrypted token.
            </span>
          {/if}
        </label>
        <label class="space-y-1.5">
          <span class="text-xs font-medium text-[color:var(--text-primary)]">CRM source name</span>
          <input
            name="lead_event_source"
            value={data.conversion?.lead_event_source || 'BottleCRM'}
            class="w-full rounded-md border border-[color:var(--border)] bg-[color:var(--bg-elevated)] px-3 py-2 text-sm"
          />
        </label>
        <label class="space-y-1.5">
          <span class="text-xs font-medium text-[color:var(--text-primary)]">
            Test event code <span class="font-normal text-[color:var(--text-muted)]"
              >(optional)</span
            >
          </span>
          <input
            name="test_event_code"
            value={data.conversion?.test_event_code || ''}
            placeholder="Leave blank for production"
            class="w-full rounded-md border border-[color:var(--border)] bg-[color:var(--bg-elevated)] px-3 py-2 text-sm"
          />
        </label>
      </div>

      <div class="grid gap-4 md:grid-cols-3">
        <label class="space-y-1.5">
          <span class="text-xs font-medium text-[color:var(--text-primary)]">Raw lead event</span>
          <input
            name="raw_lead_event_name"
            value={data.conversion?.raw_lead_event_name || 'RawLead'}
            class="w-full rounded-md border border-[color:var(--border)] bg-[color:var(--bg-elevated)] px-3 py-2 text-sm"
          />
        </label>
        <label class="space-y-1.5">
          <span class="text-xs font-medium text-[color:var(--text-primary)]">Qualified event</span>
          <input
            name="qualified_lead_event_name"
            value={data.conversion?.qualified_lead_event_name || 'MarketingQualifiedLead'}
            class="w-full rounded-md border border-[color:var(--border)] bg-[color:var(--bg-elevated)] px-3 py-2 text-sm"
          />
        </label>
        <label class="space-y-1.5">
          <span class="text-xs font-medium text-[color:var(--text-primary)]">Converted event</span>
          <input
            name="converted_event_name"
            value={data.conversion?.converted_event_name || 'Converted'}
            class="w-full rounded-md border border-[color:var(--border)] bg-[color:var(--bg-elevated)] px-3 py-2 text-sm"
          />
        </label>
      </div>

      <fieldset class="space-y-2">
        <legend class="text-xs font-medium text-[color:var(--text-primary)]">
          Qualification bands returned as qualified
        </legend>
        <div class="flex flex-wrap gap-4">
          {#each ['high', 'medium', 'low', 'disqualified'] as band}
            <label
              class="flex items-center gap-2 text-sm text-[color:var(--text-secondary)] capitalize"
            >
              <input
                type="checkbox"
                name="qualified_bands"
                value={band}
                checked={(data.conversion?.qualified_bands || ['high']).includes(band)}
                class="size-4 rounded border-[color:var(--border)] accent-blue-600"
              />
              {band}
            </label>
          {/each}
        </div>
      </fieldset>

      <div class="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {#each [['Pending', data.conversion?.event_summary?.pending || 0], ['Sent', data.conversion?.event_summary?.sent || 0], ['Failed', data.conversion?.event_summary?.failed || 0], ['Cancelled', data.conversion?.event_summary?.cancelled || 0]] as metric}
          <div class="rounded-lg bg-[color:var(--bg-subtle)] p-3">
            <p class="text-xs text-[color:var(--text-muted)]">{metric[0]}</p>
            <p class="mt-1 text-lg font-semibold text-[color:var(--text-primary)]">{metric[1]}</p>
          </div>
        {/each}
      </div>

      <div class="flex justify-end">
        <Button type="submit">Save conversion feedback</Button>
      </div>
    </form>
  </SectionCard>
</div>
