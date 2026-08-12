<script>
  import { enhance } from '$app/forms';
  import { PageHeader } from '$lib/components/layout';
  import { Button } from '$lib/components/ui/button/index.js';
  import { Input } from '$lib/components/ui/input/index.js';
  import { toast } from 'svelte-sonner';
  import {
    CheckCircle2,
    Clock3,
    Eye,
    FlaskConical,
    Mail,
    MessageCircleReply,
    MousePointerClick,
    PauseCircle,
    Pencil,
    Plus,
    RotateCcw,
    ShieldBan,
    Trash2,
    Users
  } from '@lucide/svelte';

  /** @type {{ data: any, form: any }} */
  let { data, form } = $props();

  const nurture = $derived(data.nurture || {});
  const summary = $derived(nurture.summary || {});
  const sequences = $derived(nurture.results || []);
  const enrollments = $derived(data.enrollments?.results || []);
  const suppressions = $derived(data.suppressions?.results || []);
  const suppressionReasons = $derived(data.suppressions?.reasons || []);

  let editorOpen = $state(false);
  let editingId = $state('');
  let name = $state('');
  let description = $state('');
  let priority = $state(100);
  let isActive = $state(false);
  let autoEnroll = $state(false);
  let fromEmail = $state('');
  let selectedSources = $state([]);
  let selectedBands = $state([]);
  let steps = $state([]);
  let suppressionReason = $state('admin');

  $effect(() => {
    if (form?.actionError) toast.error(form.actionError);
    if (form?.saved) {
      toast.success('Nurture sequence saved.');
      editorOpen = false;
    }
    if (form?.deleted) toast.success('Nurture sequence deleted.');
    if (form?.enrollmentUpdated) toast.success('Enrollment updated.');
    if (form?.suppressionAdded) toast.success('Email address suppressed.');
    if (form?.suppressionReleased) toast.success('Email suppression released.');
  });

  function blankStep(position = 1) {
    return {
      position,
      delay_minutes: position === 1 ? 0 : 1440,
      subject_a: '',
      body_a: '',
      subject_b: '',
      body_b: '',
      variant_b_percent: 0
    };
  }

  function openCreate() {
    editingId = '';
    name = '';
    description = '';
    priority = 100;
    isActive = false;
    autoEnroll = false;
    fromEmail = '';
    selectedSources = [];
    selectedBands = [];
    steps = [blankStep(1)];
    editorOpen = true;
  }

  /** @param {any} sequence */
  function openEdit(sequence) {
    editingId = sequence.id;
    name = sequence.name;
    description = sequence.description || '';
    priority = sequence.priority;
    isActive = sequence.is_active;
    autoEnroll = sequence.auto_enroll;
    fromEmail = sequence.from_email || '';
    selectedSources = [...(sequence.sources || [])];
    selectedBands = [...(sequence.qualification_bands || [])];
    steps = (sequence.steps || []).map((step) => ({ ...step }));
    editorOpen = true;
  }

  function addStep() {
    steps = [...steps, blankStep(steps.length + 1)];
  }

  /** @param {number} index */
  function removeStep(index) {
    if (steps.length === 1) return;
    steps = steps
      .filter((_, itemIndex) => itemIndex !== index)
      .map((step, itemIndex) => ({ ...step, position: itemIndex + 1 }));
  }

  /** @param {string[]} values @param {string} value */
  function toggle(values, value) {
    return values.includes(value) ? values.filter((item) => item !== value) : [...values, value];
  }

  /** @param {number} minutes */
  function delayLabel(minutes) {
    if (!minutes) return 'Immediately';
    if (minutes % 1440 === 0) return `${minutes / 1440} day${minutes === 1440 ? '' : 's'}`;
    if (minutes % 60 === 0) return `${minutes / 60} hour${minutes === 60 ? '' : 's'}`;
    return `${minutes} minutes`;
  }

  /** @param {string | null} value */
  function formatDate(value) {
    if (!value) return '-';
    return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(
      new Date(value)
    );
  }

  /** @param {string} status */
  function statusClass(status) {
    if (status === 'active') return 'bg-emerald-100 text-emerald-700';
    if (status === 'paused') return 'bg-amber-100 text-amber-700';
    if (status === 'replied' || status === 'converted') return 'bg-blue-100 text-blue-700';
    if (status === 'cancelled') return 'bg-red-100 text-red-700';
    return 'bg-slate-100 text-slate-600';
  }

  /** @param {string} reason */
  function suppressionReasonLabel(reason) {
    return suppressionReasons.find((item) => item.value === reason)?.label || reason;
  }
</script>

<svelte:head>
  <title>SDR Nurturing - BottleCRM</title>
</svelte:head>

<PageHeader
  title="SDR Nurturing"
  subtitle="Turn not-yet-ready inbound leads into future MQLs with durable follow-up sequences"
/>

<div class="space-y-6 p-6 md:p-8">
  <div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-4 2xl:grid-cols-9">
    <div
      class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-4"
    >
      <div class="flex items-center justify-between">
        <p class="text-xs text-[color:var(--text-muted)]">Active sequences</p>
        <Mail class="size-4 text-violet-500" />
      </div>
      <p class="mt-2 text-2xl font-semibold">{summary.active_sequences || 0}</p>
    </div>
    <div
      class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-4"
    >
      <div class="flex items-center justify-between">
        <p class="text-xs text-[color:var(--text-muted)]">Active leads</p>
        <Users class="size-4 text-blue-500" />
      </div>
      <p class="mt-2 text-2xl font-semibold">{summary.active_enrollments || 0}</p>
    </div>
    <div
      class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-4"
    >
      <div class="flex items-center justify-between">
        <p class="text-xs text-[color:var(--text-muted)]">Suppressed</p>
        <ShieldBan class="size-4 text-red-500" />
      </div>
      <p class="mt-2 text-2xl font-semibold">{summary.active_suppressions || 0}</p>
    </div>
    <div
      class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-4"
    >
      <div class="flex items-center justify-between">
        <p class="text-xs text-[color:var(--text-muted)]">Emails sent</p>
        <CheckCircle2 class="size-4 text-emerald-500" />
      </div>
      <p class="mt-2 text-2xl font-semibold">{summary.sent || 0}</p>
    </div>
    <div
      class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-4"
    >
      <div class="flex items-center justify-between">
        <p class="text-xs text-[color:var(--text-muted)]">Bounce rate</p>
        <Mail class="size-4 text-red-500" />
      </div>
      <p class="mt-2 text-2xl font-semibold">{summary.bounce_rate || 0}%</p>
    </div>
    <div
      class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-4"
    >
      <div class="flex items-center justify-between">
        <p class="text-xs text-[color:var(--text-muted)]">Open rate</p>
        <Eye class="size-4 text-sky-500" />
      </div>
      <p class="mt-2 text-2xl font-semibold">{summary.open_rate || 0}%</p>
    </div>
    <div
      class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-4"
    >
      <div class="flex items-center justify-between">
        <p class="text-xs text-[color:var(--text-muted)]">Click rate</p>
        <MousePointerClick class="size-4 text-indigo-500" />
      </div>
      <p class="mt-2 text-2xl font-semibold">{summary.click_rate || 0}%</p>
    </div>
    <div
      class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-4"
    >
      <div class="flex items-center justify-between">
        <p class="text-xs text-[color:var(--text-muted)]">Reply rate</p>
        <MessageCircleReply class="size-4 text-cyan-500" />
      </div>
      <p class="mt-2 text-2xl font-semibold">{summary.reply_rate || 0}%</p>
    </div>
    <div
      class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-4"
    >
      <div class="flex items-center justify-between">
        <p class="text-xs text-[color:var(--text-muted)]">Positive replies</p>
        <FlaskConical class="size-4 text-fuchsia-500" />
      </div>
      <p class="mt-2 text-2xl font-semibold">{summary.positive_reply_rate || 0}%</p>
    </div>
  </div>

  <section
    class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)]"
  >
    <div
      class="flex flex-wrap items-center justify-between gap-3 border-b border-[color:var(--border-faint)] p-4"
    >
      <div>
        <h2 class="font-medium text-[color:var(--text-primary)]">Follow-up sequences</h2>
        <p class="text-xs text-[color:var(--text-muted)]">
          The first active rule matching source and score band enrolls each lead once.
        </p>
      </div>
      <Button onclick={openCreate} class="gap-1.5"><Plus class="size-4" />New sequence</Button>
    </div>

    {#if sequences.length}
      <ul class="divide-y divide-[color:var(--border-faint)]">
        {#each sequences as sequence (sequence.id)}
          <li class="p-4">
            <div class="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
              <div class="min-w-0 space-y-3">
                <div class="flex flex-wrap items-center gap-2">
                  <span class="rounded bg-[color:var(--bg-subtle)] px-2 py-0.5 text-xs font-medium"
                    >#{sequence.priority}</span
                  >
                  <h3 class="font-medium text-[color:var(--text-primary)]">{sequence.name}</h3>
                  <span
                    class="rounded-full px-2 py-0.5 text-[11px] {sequence.is_active
                      ? 'bg-emerald-100 text-emerald-700'
                      : 'bg-slate-100 text-slate-500'}"
                  >
                    {sequence.is_active ? 'Active' : 'Disabled'}
                  </span>
                  {#if sequence.auto_enroll}
                    <span class="rounded-full bg-violet-100 px-2 py-0.5 text-[11px] text-violet-700"
                      >Auto enroll</span
                    >
                  {/if}
                </div>
                {#if sequence.description}
                  <p class="text-xs text-[color:var(--text-muted)]">{sequence.description}</p>
                {/if}
                <div class="flex flex-wrap gap-2 text-xs text-[color:var(--text-muted)]">
                  {#each sequence.steps as step (step.id)}
                    <span class="rounded-md border border-[color:var(--border-faint)] px-2 py-1">
                      Step {step.position}: {delayLabel(step.delay_minutes)}
                      {#if step.variant_b_percent}
                        · A/B {100 - step.variant_b_percent}/{step.variant_b_percent}{/if}
                    </span>
                  {/each}
                </div>
                <div class="grid gap-3 text-xs sm:grid-cols-4 2xl:grid-cols-8">
                  <p>
                    <span class="text-[color:var(--text-muted)]">Enrolled</span><strong class="ml-2"
                      >{sequence.metrics.enrollments}</strong
                    >
                  </p>
                  <p>
                    <span class="text-[color:var(--text-muted)]">Sent</span><strong class="ml-2"
                      >{sequence.metrics.sent}</strong
                    >
                  </p>
                  <p>
                    <span class="text-[color:var(--text-muted)]">Bounces</span><strong class="ml-2"
                      >{sequence.metrics.bounced}</strong
                    >
                  </p>
                  <p>
                    <span class="text-[color:var(--text-muted)]">Opens</span><strong class="ml-2"
                      >{sequence.metrics.opened}</strong
                    >
                  </p>
                  <p>
                    <span class="text-[color:var(--text-muted)]">Clicks</span><strong class="ml-2"
                      >{sequence.metrics.clicked}</strong
                    >
                  </p>
                  <p>
                    <span class="text-[color:var(--text-muted)]">Replies</span><strong class="ml-2"
                      >{sequence.metrics.replied}</strong
                    >
                  </p>
                  <p>
                    <span class="text-[color:var(--text-muted)]">A reply</span><strong class="ml-2"
                      >{sequence.metrics.variants.A.reply_rate}%</strong
                    >
                  </p>
                  <p>
                    <span class="text-[color:var(--text-muted)]">B reply</span><strong class="ml-2"
                      >{sequence.metrics.variants.B.reply_rate}%</strong
                    >
                  </p>
                </div>
              </div>
              <div class="flex shrink-0 gap-2">
                <Button size="sm" variant="outline" onclick={() => openEdit(sequence)} class="gap-1"
                  ><Pencil class="size-3.5" />Edit</Button
                >
                <form method="POST" action="?/delete" use:enhance>
                  <input type="hidden" name="id" value={sequence.id} />
                  <Button
                    type="submit"
                    size="icon"
                    variant="ghost"
                    class="text-red-600"
                    aria-label="Delete sequence"><Trash2 class="size-4" /></Button
                  >
                </form>
              </div>
            </div>
          </li>
        {/each}
      </ul>
    {:else}
      <div class="p-10 text-center">
        <Mail class="mx-auto size-8 text-[color:var(--text-muted)]" />
        <p class="mt-3 text-sm font-medium">No nurture sequence yet</p>
        <p class="mt-1 text-xs text-[color:var(--text-muted)]">
          Create one disabled, review the copy, then enable automatic enrollment.
        </p>
      </div>
    {/if}
  </section>

  {#if editorOpen}
    <section
      class="rounded-lg border border-violet-200 bg-[color:var(--bg-elevated)] p-5 shadow-sm"
    >
      <div class="mb-5 flex items-center justify-between gap-3">
        <div>
          <h2 class="font-medium">
            {editingId ? 'Edit nurture sequence' : 'Create nurture sequence'}
          </h2>
          <p class="text-xs text-[color:var(--text-muted)]">
            Templates support first_name, last_name, company_name, organization_name,
            qualification_band, and qualification_score.
          </p>
        </div>
        <Button variant="ghost" onclick={() => (editorOpen = false)}>Close</Button>
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
        <input type="hidden" name="steps" value={JSON.stringify(steps)} />

        <div class="grid gap-4 md:grid-cols-[1fr_120px_1fr]">
          <label class="space-y-1.5 text-sm"
            ><span class="font-medium">Name</span><Input
              name="name"
              bind:value={name}
              maxlength="160"
              required
            /></label
          >
          <label class="space-y-1.5 text-sm"
            ><span class="font-medium">Priority</span><Input
              name="priority"
              type="number"
              min="0"
              bind:value={priority}
              required
            /></label
          >
          <label class="space-y-1.5 text-sm"
            ><span class="font-medium">From email</span><Input
              name="from_email"
              type="email"
              bind:value={fromEmail}
              placeholder="Platform default"
            /></label
          >
        </div>
        <label class="block space-y-1.5 text-sm"
          ><span class="font-medium">Description</span><textarea
            name="description"
            bind:value={description}
            rows="2"
            class="w-full rounded-md border border-[color:var(--border-faint)] bg-transparent px-3 py-2"
          ></textarea></label
        >

        <div class="grid gap-4 lg:grid-cols-2">
          <fieldset class="rounded-md border border-[color:var(--border-faint)] p-3">
            <legend class="px-1 text-xs font-medium">Sources · none means any</legend>
            <div class="mt-2 flex flex-wrap gap-3">
              {#each nurture.sources || [] as source (source.value)}
                <label class="flex items-center gap-1.5 text-xs"
                  ><input
                    type="checkbox"
                    checked={selectedSources.includes(source.value)}
                    onchange={() => (selectedSources = toggle(selectedSources, source.value))}
                  />{source.label}</label
                >
              {/each}
            </div>
          </fieldset>
          <fieldset class="rounded-md border border-[color:var(--border-faint)] p-3">
            <legend class="px-1 text-xs font-medium">Qualification · none means any</legend>
            <div class="mt-2 flex flex-wrap gap-3">
              {#each nurture.qualification_bands || [] as band (band.value)}
                <label class="flex items-center gap-1.5 text-xs"
                  ><input
                    type="checkbox"
                    checked={selectedBands.includes(band.value)}
                    onchange={() => (selectedBands = toggle(selectedBands, band.value))}
                  />{band.label}</label
                >
              {/each}
            </div>
          </fieldset>
        </div>

        <div class="space-y-3">
          <div class="flex items-center justify-between">
            <h3 class="text-sm font-medium">Sequence steps</h3>
            <Button type="button" size="sm" variant="outline" onclick={addStep} class="gap-1"
              ><Plus class="size-3.5" />Add step</Button
            >
          </div>
          {#each steps as step, index (step.position)}
            <div class="space-y-3 rounded-lg border border-[color:var(--border-faint)] p-4">
              <div class="flex items-center justify-between">
                <div class="flex items-center gap-2">
                  <Clock3 class="size-4 text-violet-500" /><strong class="text-sm"
                    >Step {step.position}</strong
                  >
                </div>
                <Button
                  type="button"
                  size="icon"
                  variant="ghost"
                  disabled={steps.length === 1}
                  onclick={() => removeStep(index)}
                  aria-label="Remove step"><Trash2 class="size-4" /></Button
                >
              </div>
              <label class="block max-w-xs space-y-1 text-xs"
                ><span>Delay after {index === 0 ? 'enrollment' : 'previous send'} (minutes)</span
                ><input
                  type="number"
                  min="0"
                  max="525600"
                  bind:value={step.delay_minutes}
                  class="h-9 w-full rounded-md border border-[color:var(--border-faint)] bg-transparent px-3"
                /></label
              >
              <div class="grid gap-4 lg:grid-cols-2">
                <div class="space-y-2 rounded-md bg-[color:var(--bg-subtle)] p-3">
                  <p class="text-xs font-medium">Variant A</p>
                  <input
                    bind:value={step.subject_a}
                    maxlength="255"
                    required
                    placeholder="Subject"
                    class="h-9 w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] px-3 text-sm"
                  />
                  <textarea
                    bind:value={step.body_a}
                    rows="5"
                    required
                    placeholder="Email body"
                    class="w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] px-3 py-2 text-sm"
                  ></textarea>
                </div>
                <div class="space-y-2 rounded-md bg-[color:var(--bg-subtle)] p-3">
                  <div class="flex items-center justify-between gap-3">
                    <p class="text-xs font-medium">Variant B · optional</p>
                    <label class="flex items-center gap-2 text-xs"
                      ><span>Traffic %</span><input
                        type="number"
                        min="0"
                        max="100"
                        bind:value={step.variant_b_percent}
                        class="h-8 w-20 rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] px-2"
                      /></label
                    >
                  </div>
                  <input
                    bind:value={step.subject_b}
                    maxlength="255"
                    placeholder="Alternative subject"
                    class="h-9 w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] px-3 text-sm"
                  />
                  <textarea
                    bind:value={step.body_b}
                    rows="5"
                    placeholder="Alternative body"
                    class="w-full rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] px-3 py-2 text-sm"
                  ></textarea>
                </div>
              </div>
            </div>
          {/each}
        </div>

        <div
          class="flex flex-wrap items-center justify-between gap-4 border-t border-[color:var(--border-faint)] pt-4"
        >
          <div class="flex flex-wrap gap-4 text-sm">
            <label class="flex items-center gap-2"
              ><input type="checkbox" name="is_active" bind:checked={isActive} />Sequence enabled</label
            >
            <label class="flex items-center gap-2"
              ><input type="checkbox" name="auto_enroll" bind:checked={autoEnroll} />Automatically
              enroll matching leads</label
            >
          </div>
          <Button type="submit">{editingId ? 'Save sequence' : 'Create sequence'}</Button>
        </div>
      </form>
    </section>
  {/if}

  <section
    class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)]"
  >
    <div class="border-b border-[color:var(--border-faint)] p-4">
      <h2 class="font-medium text-[color:var(--text-primary)]">Recent enrollments</h2>
      <p class="text-xs text-[color:var(--text-muted)]">
        Pause, resume, or stop follow-up as sales learns more.
      </p>
    </div>
    {#if enrollments.length}
      <div class="overflow-x-auto">
        <table class="w-full min-w-[1000px] text-left text-sm">
          <thead class="bg-[color:var(--bg-subtle)] text-xs text-[color:var(--text-muted)]"
            ><tr
              ><th class="px-4 py-3 font-medium">Lead</th><th class="px-4 py-3 font-medium"
                >Sequence</th
              ><th class="px-4 py-3 font-medium">Progress</th><th class="px-4 py-3 font-medium"
                >Next run</th
              ><th class="px-4 py-3 font-medium">Status</th><th class="px-4 py-3 font-medium"
                >Action</th
              ></tr
            ></thead
          >
          <tbody class="divide-y divide-[color:var(--border-faint)]">
            {#each enrollments as enrollment (enrollment.id)}
              <tr class="align-top">
                <td class="px-4 py-3"
                  ><a
                    href="/leads/{enrollment.lead_id}"
                    class="font-medium text-violet-600 hover:underline"
                    >{enrollment.company_name || enrollment.contact_name || 'Lead'}</a
                  >
                  <p class="mt-1 text-xs text-[color:var(--text-muted)]">
                    {enrollment.contact_email}
                  </p></td
                >
                <td class="px-4 py-3">{enrollment.sequence_name}</td>
                <td class="px-4 py-3"
                  >Step {enrollment.current_step_position}
                  <p class="mt-1 text-xs text-[color:var(--text-muted)]">
                    {enrollment.deliveries.filter((item) => item.status === 'sent').length} sent
                  </p></td
                >
                <td class="px-4 py-3 text-xs text-[color:var(--text-muted)]"
                  >{formatDate(enrollment.next_run_at)}</td
                >
                <td class="px-4 py-3"
                  ><span
                    class="rounded-full px-2 py-0.5 text-[11px] {statusClass(enrollment.status)}"
                    >{enrollment.status}</span
                  >{#if enrollment.stop_reason}<p
                      class="mt-1 max-w-[200px] text-xs text-[color:var(--text-muted)]"
                    >
                      {enrollment.stop_reason}
                    </p>{/if}</td
                >
                <td class="px-4 py-3">
                  {#if enrollment.status === 'active' || enrollment.status === 'paused'}
                    <form
                      method="POST"
                      action="?/enrollmentAction"
                      use:enhance
                      class="flex items-center gap-2"
                    >
                      <input type="hidden" name="id" value={enrollment.id} />
                      <select
                        name="enrollment_action"
                        class="h-8 rounded-md border border-[color:var(--border-faint)] bg-transparent px-2 text-xs"
                      >
                        <option value={enrollment.status === 'paused' ? 'resume' : 'pause'}
                          >{enrollment.status === 'paused' ? 'Resume' : 'Pause'}</option
                        >
                        <option value="mark_replied">Mark replied</option>
                        <option value="mark_converted">Mark converted</option>
                        <option value="cancel">Cancel</option>
                      </select>
                      <select
                        name="reply_sentiment"
                        aria-label="Reply sentiment"
                        class="h-8 rounded-md border border-[color:var(--border-faint)] bg-transparent px-2 text-xs"
                        ><option value="positive">Positive</option><option value="neutral"
                          >Neutral</option
                        ><option value="negative">Negative</option></select
                      >
                      <Button type="submit" size="sm" variant="outline" class="gap-1"
                        ><PauseCircle class="size-3.5" />Apply</Button
                      >
                    </form>
                  {:else}
                    <span class="text-xs text-[color:var(--text-muted)]">No action needed</span>
                  {/if}
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {:else}
      <div class="p-10 text-center text-sm text-[color:var(--text-muted)]">
        No leads have entered a nurture sequence yet.
      </div>
    {/if}
  </section>

  <section
    class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)]"
  >
    <div
      class="flex flex-col gap-4 border-b border-[color:var(--border-faint)] p-4 lg:flex-row lg:items-end lg:justify-between"
    >
      <div>
        <h2 class="font-medium text-[color:var(--text-primary)]">Email suppression list</h2>
        <p class="text-xs text-[color:var(--text-muted)]">
          One-click and reply opt-outs stop every current sequence and prevent future automatic
          enrollment.
        </p>
      </div>
      <form method="POST" action="?/addSuppression" use:enhance class="flex flex-wrap gap-2">
        <Input name="email" type="email" required placeholder="person@example.com" class="w-60" />
        <select
          name="reason"
          bind:value={suppressionReason}
          class="h-9 rounded-md border border-[color:var(--border-faint)] bg-transparent px-3 text-sm"
        >
          {#each suppressionReasons as reason (reason.value)}
            <option value={reason.value}>{reason.label}</option>
          {/each}
        </select>
        <Button type="submit" variant="outline" class="gap-1.5"
          ><ShieldBan class="size-4" />Suppress</Button
        >
      </form>
    </div>
    {#if suppressions.length}
      <div class="overflow-x-auto">
        <table class="w-full min-w-[760px] text-left text-sm">
          <thead class="bg-[color:var(--bg-subtle)] text-xs text-[color:var(--text-muted)]">
            <tr>
              <th class="px-4 py-3 font-medium">Email</th>
              <th class="px-4 py-3 font-medium">Reason</th>
              <th class="px-4 py-3 font-medium">Source</th>
              <th class="px-4 py-3 font-medium">Suppressed</th>
              <th class="px-4 py-3 font-medium">Action</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-[color:var(--border-faint)]">
            {#each suppressions as suppression (suppression.id)}
              <tr>
                <td class="px-4 py-3 font-medium">{suppression.email}</td>
                <td class="px-4 py-3">{suppressionReasonLabel(suppression.reason)}</td>
                <td class="px-4 py-3 text-xs text-[color:var(--text-muted)]"
                  >{suppression.source.replaceAll('_', ' ')}</td
                >
                <td class="px-4 py-3 text-xs text-[color:var(--text-muted)]"
                  >{formatDate(suppression.suppressed_at)}</td
                >
                <td class="px-4 py-3">
                  <form method="POST" action="?/releaseSuppression" use:enhance>
                    <input type="hidden" name="id" value={suppression.id} />
                    <Button type="submit" size="sm" variant="ghost" class="gap-1.5"
                      ><RotateCcw class="size-3.5" />Release</Button
                    >
                  </form>
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {:else}
      <div class="p-8 text-center text-sm text-[color:var(--text-muted)]">
        No active email suppressions.
      </div>
    {/if}
  </section>
</div>
