<script>
  import { PageHeader } from '$lib/components/layout';
  import { AlertTriangle, CheckCircle2, LockKeyhole, ShieldCheck } from '@lucide/svelte';
  let { data, form } = $props();
  const safety = $derived(data.safety);
  const focusedUnknownRequestId = $derived(data.focusedUnknownRequestId || '');
  const focusedUnknownRequestPresent = $derived(
    safety.unknownRequests.some((item) => item.id === focusedUnknownRequestId)
  );
  const label = (value) =>
    ({
      email: 'Email',
      whatsapp: 'WhatsApp',
      linkedin: 'LinkedIn',
      feishu: 'Feishu',
      apollo: 'Apollo',
      facebook: 'Facebook',
      wechat: 'WeChat',
      wecom: 'WeCom'
    })[value] || value;
  const date = (value) =>
    value
      ? new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(
          new Date(value)
        )
      : '—';
  let copiedApprovalId = $state('');
  let copiedRequestId = $state('');
  async function copyApprovalId(value) {
    if (!globalThis.navigator?.clipboard) return;
    await globalThis.navigator.clipboard.writeText(value);
    copiedApprovalId = value;
  }
  async function copyRequestId(value) {
    if (!globalThis.navigator?.clipboard) return;
    await globalThis.navigator.clipboard.writeText(value);
    copiedRequestId = value;
  }
  /**
   * @param {SubmitEvent} event
   * @param {'delivered' | 'failed_consumed'} outcome
   */
  function confirmUnknownResolution(event, outcome) {
    const message =
      outcome === 'delivered'
        ? 'Permanently settle this request as delivered and keep its quota consumed?'
        : 'Permanently settle this request as failed with its quota consumed?';
    if (!globalThis.confirm(message)) event.preventDefault();
  }
</script>

<svelte:head><title>Channel Safety - BottleCRM</title></svelte:head>
<PageHeader
  title="Real-channel safety"
  subtitle="Configure guardrails and reconcile execution state. This console never sends messages."
>
  {#snippet titleIcon()}<ShieldCheck class="size-4" />{/snippet}
</PageHeader>

<main class="space-y-6 p-6 md:p-8">
  {#if data.loadError || form?.actionError}
    <div
      role="alert"
      class="flex gap-3 rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-800"
    >
      <AlertTriangle class="size-4 shrink-0" /><span>{form?.actionError || data.loadError}</span>
    </div>
  {/if}
  {#if form?.saved || form?.targetSaved || form?.approvalSaved || form?.reconciled}
    <div
      role="status"
      class="flex gap-3 rounded-lg border border-emerald-200 bg-emerald-50 p-4 text-sm text-emerald-800"
    >
      <CheckCircle2 class="size-4 shrink-0" /><span>Safety control updated.</span>
    </div>
  {/if}
  {#if form?.approvalSaved && form?.issuedApprovalId}
    <div
      role="status"
      class="rounded-lg border border-emerald-200 bg-emerald-50 p-4 text-sm text-emerald-950"
    >
      <p class="font-semibold">One-time approval issued</p>
      <p class="mt-1 text-xs">
        Copy this non-PII approval UUID back to the exact provider action that supplied the
        fingerprint. Issuing it did not execute anything.
      </p>
      <div class="mt-3 flex flex-col gap-2 sm:flex-row">
        <input
          aria-label="Issued approval UUID"
          readonly
          value={form.issuedApprovalId}
          class="h-9 min-w-0 flex-1 rounded-md border border-emerald-300 bg-white px-3 font-mono text-xs"
        />
        <button
          class="rounded-md border border-emerald-400 px-3 py-2 text-xs font-medium"
          type="button"
          onclick={() => copyApprovalId(form.issuedApprovalId)}
          >{copiedApprovalId === form.issuedApprovalId ? 'Copied' : 'Copy UUID'}</button
        >
      </div>
    </div>
  {/if}
  <div class="rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900">
    <strong>Safety boundary:</strong> provider execution also requires the environment gate, organization
    gate, channel gate, limits, an active test target, and an exact one-time approval. No send action
    exists on this page.
  </div>

  <section
    class="rounded-xl border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-5"
  >
    <div class="mb-4 flex items-center justify-between gap-3">
      <div>
        <h2 class="font-semibold">Organization kill switch</h2>
        <p class="text-xs text-[color:var(--text-muted)]">
          Environment gate: {safety.environmentEnabled ? 'enabled' : 'disabled'}. Used {safety
            .organization.consumedUnits}; reserved {safety.organization.reservedUnits}.
        </p>
      </div>
      <LockKeyhole class="size-5" />
    </div>
    <form
      method="POST"
      action="?/saveOrganization"
      class="grid gap-4 sm:grid-cols-[1fr_1fr_auto] sm:items-end"
    >
      <label class="text-sm"
        ><span class="mb-1 block">Daily organization limit</span><input
          class="w-full rounded-md border bg-transparent px-3 py-2"
          name="daily_limit"
          type="number"
          min="0"
          max="10000000"
          value={safety.organization.dailyLimit}
          required
        /></label
      >
      <label class="flex min-h-10 items-center gap-2 text-sm"
        ><input name="enabled" type="checkbox" checked={safety.organization.enabled} /> Allow guarded
        execution</label
      >
      <input type="hidden" name="expected_revision" value={safety.organization.revision} />
      <button class="rounded-md bg-slate-900 px-4 py-2 text-sm text-white" type="submit"
        >Save guard</button
      >
    </form>
  </section>

  <section aria-labelledby="channels-title">
    <h2 id="channels-title" class="mb-3 font-semibold">Channel controls</h2>
    <div class="grid gap-4 lg:grid-cols-2">
      {#each safety.channels as channel (channel.channel)}
        <form
          method="POST"
          action="?/saveChannel"
          class="rounded-xl border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-5"
        >
          <div class="mb-4 flex items-center justify-between">
            <h3 class="font-medium">{label(channel.channel)}</h3>
            <span
              class="rounded-full px-2 py-1 text-xs {channel.implemented
                ? 'bg-emerald-100 text-emerald-800'
                : 'bg-slate-100 text-slate-600'}"
              >{channel.implemented ? (channel.enabled ? 'Guarded on' : 'Off') : 'Disabled'}</span
            >
          </div>
          {#if !channel.implemented}<p class="text-sm text-[color:var(--text-muted)]">
              This channel is not implemented and cannot be enabled.
            </p>
          {:else}<div class="grid gap-3 sm:grid-cols-2">
              <label class="text-sm"
                ><span class="mb-1 block">Daily limit</span><input
                  class="w-full rounded-md border bg-transparent px-3 py-2"
                  name="daily_limit"
                  type="number"
                  min="0"
                  max="1000000"
                  value={channel.dailyLimit}
                  required
                /></label
              >
              <label class="text-sm"
                ><span class="mb-1 block">Per execution</span><input
                  class="w-full rounded-md border bg-transparent px-3 py-2"
                  name="per_execution_limit"
                  type="number"
                  min="0"
                  max="1000000"
                  value={channel.perExecutionLimit}
                  required
                /></label
              >
              <label class="flex items-center gap-2 text-sm"
                ><input name="enabled" type="checkbox" checked={channel.enabled} /> Channel enabled</label
              >
              <label class="flex items-center gap-2 text-sm"
                ><input name="test_mode" type="checkbox" checked={channel.testMode} /> Test targets only</label
              >
            </div>
            <p class="mt-3 text-xs text-[color:var(--text-muted)]">
              Used {channel.consumedUnits}; reserved {channel.reservedUnits}.
            </p>
            <input type="hidden" name="channel" value={channel.channel} /><input
              type="hidden"
              name="expected_revision"
              value={channel.revision}
            />
            <button class="mt-4 rounded-md border px-3 py-2 text-sm" type="submit"
              >Save channel guard</button
            >{/if}
        </form>
      {/each}
    </div>
  </section>

  <section class="grid gap-6 xl:grid-cols-2">
    <div
      class="rounded-xl border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-5"
    >
      <h2 class="font-semibold">Masked test targets</h2>
      <p class="mt-1 text-xs text-[color:var(--text-muted)]">
        The identifier is submitted once and never returned. Use a label that cannot identify the
        recipient.
      </p>
      <form method="POST" action="?/addTarget" class="mt-4 grid gap-3">
        <label class="text-sm"
          >Channel<select
            name="channel"
            class="mt-1 w-full rounded-md border bg-transparent px-3 py-2"
            required
            ><option value="">Select</option
            >{#each safety.channels.filter((c) => c.implemented) as c}<option value={c.channel}
                >{label(c.channel)}</option
              >{/each}</select
          ></label
        >
        <label class="text-sm"
          >Private identifier<input
            name="identifier"
            type="password"
            autocomplete="off"
            class="mt-1 w-full rounded-md border bg-transparent px-3 py-2"
            required
          /></label
        >
        <label class="text-sm"
          >Masked label<input
            name="safe_label"
            maxlength="120"
            placeholder="QA recipient ••••42"
            class="mt-1 w-full rounded-md border bg-transparent px-3 py-2"
            required
          /></label
        >
        <button class="justify-self-start rounded-md border px-3 py-2 text-sm" type="submit"
          >Add test target</button
        >
      </form>
      <ul class="mt-5 divide-y">
        {#each safety.testTargets as target (target.id)}<li
            class="flex items-center justify-between gap-3 py-3 text-sm"
          >
            <span>{label(target.channel)} · {target.safeLabel}</span>{#if target.active}<form
                method="POST"
                action="?/disableTarget"
              >
                <input type="hidden" name="target_id" value={target.id} /><button
                  class="text-red-700 underline"
                  type="submit">Disable</button
                >
              </form>{/if}
          </li>{/each}
      </ul>
    </div>

    <div
      class="rounded-xl border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-5"
    >
      <h2 class="font-semibold">One-time approvals</h2>
      <p class="mt-1 text-xs text-[color:var(--text-muted)]">
        Approval is short-lived, single-use and bound to an exact test target and payload
        fingerprint. It does not execute anything.
      </p>
      <form method="POST" action="?/approve" class="mt-4 grid gap-3 sm:grid-cols-2">
        <label class="text-sm sm:col-span-2"
          >Test target<select
            name="target_id"
            class="mt-1 w-full rounded-md border bg-transparent px-3 py-2"
            required
            ><option value="">Select</option
            >{#each safety.testTargets.filter((t) => t.active) as t}<option value={t.id}
                >{label(t.channel)} · {t.safeLabel}</option
              >{/each}</select
          ></label
        >
        <label class="text-sm"
          >Action<input
            name="approval_action"
            maxlength="64"
            class="mt-1 w-full rounded-md border bg-transparent px-3 py-2"
            required
          /></label
        >
        <label class="text-sm"
          >Units<input
            name="units"
            type="number"
            min="1"
            max="1000000"
            value="1"
            class="mt-1 w-full rounded-md border bg-transparent px-3 py-2"
            required
          /></label
        >
        <label class="text-sm sm:col-span-2"
          >Private payload fingerprint<input
            name="payload_sha256"
            type="password"
            autocomplete="off"
            minlength="64"
            maxlength="64"
            pattern="[0-9a-fA-F]{64}"
            class="mt-1 w-full rounded-md border bg-transparent px-3 py-2"
            required
          /></label
        >
        <label class="text-sm"
          >Expires in seconds<input
            name="expires_in_seconds"
            type="number"
            min="60"
            max="86400"
            value="900"
            class="mt-1 w-full rounded-md border bg-transparent px-3 py-2"
            required
          /></label
        >
        <button class="self-end rounded-md border px-3 py-2 text-sm" type="submit"
          >Issue approval only</button
        >
      </form>
      <ul class="mt-5 divide-y">
        {#each safety.approvals as approval (approval.id)}<li class="py-3 text-sm">
            <div>{label(approval.channel)} · {approval.safeLabel}</div>
            <div class="text-xs text-[color:var(--text-muted)]">
              {approval.action} · {approval.units} unit(s) · expires {date(approval.expiresAt)}
            </div>
            <div class="mt-2 flex items-center gap-2">
              <code
                class="min-w-0 flex-1 rounded bg-[color:var(--bg-muted)] px-2 py-1 text-[11px] break-all select-all"
                >{approval.id}</code
              ><button
                class="shrink-0 text-xs underline"
                type="button"
                onclick={() => copyApprovalId(approval.id)}
                >{copiedApprovalId === approval.id ? 'Copied' : 'Copy UUID'}</button
              >
            </div>
          </li>{/each}
      </ul>
    </div>
  </section>

  <section
    class="rounded-xl border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-5"
  >
    <h2 class="font-semibold">UNKNOWN reconciliation</h2>
    <p class="mt-1 text-xs text-[color:var(--text-muted)]">
      Resolve only after checking the provider independently. This list contains no recipient or
      provider payload.
    </p>
    {#if focusedUnknownRequestId}
      <div
        role="status"
        class="mt-4 rounded-lg border border-amber-300 bg-amber-50 p-3 text-xs text-amber-950"
      >
        <p class="font-semibold">
          {focusedUnknownRequestPresent
            ? 'Deep-linked UNKNOWN request found'
            : 'Deep-linked request is not in the current UNKNOWN queue'}
        </p>
        <div class="mt-2 flex flex-col gap-2 sm:flex-row sm:items-center">
          <code class="min-w-0 flex-1 rounded bg-white px-2 py-1 break-all select-all"
            >{focusedUnknownRequestId}</code
          >
          <button
            class="self-start font-medium underline sm:self-auto"
            type="button"
            onclick={() => copyRequestId(focusedUnknownRequestId)}
            >{copiedRequestId === focusedUnknownRequestId ? 'Copied' : 'Copy request UUID'}</button
          >
        </div>
      </div>
    {/if}
    {#if safety.unknownRequests.length === 0}<p class="mt-4 text-sm text-[color:var(--text-muted)]">
        No requests need reconciliation.
      </p>{:else}<div class="mt-4 space-y-3">
        {#each safety.unknownRequests as item (item.id)}<div
            class={`flex flex-col justify-between gap-3 rounded-lg border p-4 sm:flex-row sm:items-center ${item.id === focusedUnknownRequestId ? 'border-amber-500 bg-amber-50 ring-2 ring-amber-200' : ''}`}
          >
            <div class="text-sm">
              <strong>{label(item.channel)} · {item.action}</strong>
              <div class="text-xs text-[color:var(--text-muted)]">
                {item.units} unit(s) · unknown {date(item.unknownAt)}
              </div>
              <div class="mt-2 flex flex-col gap-1 sm:flex-row sm:items-center sm:gap-2">
                <code class="text-[11px] break-all select-all">{item.id}</code>
                <button
                  class="self-start text-xs underline sm:self-auto"
                  type="button"
                  onclick={() => copyRequestId(item.id)}
                  >{copiedRequestId === item.id ? 'Copied' : 'Copy request UUID'}</button
                >
              </div>
            </div>
            <div class="flex gap-2">
              <form
                method="POST"
                action="?/resolveUnknown"
                onsubmit={(event) => confirmUnknownResolution(event, 'delivered')}
              >
                <input type="hidden" name="request_id" value={item.id} /><input
                  type="hidden"
                  name="outcome"
                  value="delivered"
                /><button class="rounded-md border px-3 py-2 text-sm" type="submit"
                  >Confirm delivered</button
                >
              </form>
              <form
                method="POST"
                action="?/resolveUnknown"
                onsubmit={(event) => confirmUnknownResolution(event, 'failed_consumed')}
              >
                <input type="hidden" name="request_id" value={item.id} /><input
                  type="hidden"
                  name="outcome"
                  value="failed_consumed"
                /><button class="rounded-md border px-3 py-2 text-sm" type="submit"
                  >Confirm failed, quota consumed</button
                >
              </form>
            </div>
          </div>{/each}
      </div>{/if}
  </section>
</main>
