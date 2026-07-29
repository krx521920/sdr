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
    Megaphone,
    RefreshCw,
    ShieldCheck,
    Trash2
  } from '@lucide/svelte';

  /** @type {{ data: any, form: any }} */
  let { data, form } = $props();

  let disconnectingId = $state('');

  $effect(() => {
    if (data.connected) toast.success('Facebook Page connected successfully.');
    if (data.oauthError) toast.error(oauthErrorMessage(data.oauthError));
    if (data.connectionError) toast.error(data.connectionError);
    if (form?.actionError) toast.error(form.actionError);
    if (form?.disconnected) toast.success('Facebook Page disconnected.');
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
            class="flex flex-col gap-4 rounded-lg border border-[color:var(--border-faint)] p-4 sm:flex-row sm:items-center"
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
</div>
