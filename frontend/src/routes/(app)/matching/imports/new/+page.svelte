<script>
  import { enhance } from '$app/forms';
  import { goto } from '$app/navigation';
  import { resolve } from '$app/paths';
  import { onDestroy, tick } from 'svelte';
  import {
    ArrowLeft,
    Database,
    FileSpreadsheet,
    Loader2,
    Mail,
    Search,
    Upload,
    Users
  } from '@lucide/svelte';

  import { PageHeader } from '$lib/components/layout';
  import { Button } from '$lib/components/ui/button/index.js';
  import { StageStepper } from '$lib/components/ui/stage-stepper/index.js';
  import {
    FEISHU_PERSON_IMPORT_FIELDS,
    validateFeishuPersonImportMapping
  } from '$lib/feishu-base-import.js';
  import {
    autoMapImportHeaders,
    MATCHING_CRM_ENTITY_TYPES,
    MATCHING_IMPORT_FIELDS,
    MATCHING_IMPORT_MAX_BYTES,
    parseCsvHeaders,
    validateCrmImportSelection,
    validateImportMapping
  } from '$lib/matching/imports.js';

  let { data } = $props();

  const csvStages = [
    { value: 'upload', label: 'Upload', meta: 'CSV' },
    { value: 'mapping', label: 'Map', meta: 'Columns' },
    { value: 'preview', label: 'Preview', meta: 'Rows' },
    { value: 'commit', label: 'Commit', meta: 'Confirm' },
    { value: 'progress', label: 'Progress', meta: 'Import' },
    { value: 'review', label: 'Review', meta: 'Conflicts' }
  ];
  const crmStages = [
    { value: 'select', label: 'Select', meta: 'CRM records' },
    { value: 'preview', label: 'Preview', meta: 'People' },
    { value: 'commit', label: 'Commit', meta: 'Confirm' },
    { value: 'progress', label: 'Progress', meta: 'Import' },
    { value: 'review', label: 'Review', meta: 'Conflicts' }
  ];
  const feishuStages = [
    { value: 'configure', label: 'Configure', meta: 'One-time map' },
    { value: 'approve', label: 'Approve', meta: 'Channel Safety' },
    { value: 'queue', label: 'Queue', meta: 'Async read' },
    { value: 'preview', label: 'Preview', meta: 'Safe batch' },
    { value: 'commit', label: 'Commit', meta: 'Confirm' }
  ];
  const emailStages = [
    { value: 'receive', label: 'Receive', meta: 'SES push' },
    { value: 'preview', label: 'Preview', meta: 'Safe receipt' },
    { value: 'commit', label: 'Commit', meta: 'Human review' },
    { value: 'progress', label: 'Progress', meta: 'Import' },
    { value: 'review', label: 'Review', meta: 'Conflicts' }
  ];
  const fieldGroups = ['Person', 'Identity', 'Evidence'];
  const feishuFieldGroups = ['Person', 'Identity', 'Evidence'];

  /** @type {File | null} */
  let file = $state(null);
  /** @type {HTMLInputElement | null} */
  let fileInput = $state(null);
  let headers = $state(/** @type {string[]} */ ([]));
  let mapping = $state(/** @type {Record<string, string>} */ ({}));
  let step = $state('upload');
  let busy = $state(false);
  let dragOver = $state(false);
  let actionError = $state('');
  let errorElement = $state(/** @type {HTMLElement | null} */ (null));
  let idempotencyKey = $state('');
  let crmIdempotencyKey = $state('');
  let selectedCrmIds = $state(/** @type {string[]} */ ([]));
  let presentedCrmQuery = $state('');
  let feishuMapping = $state(/** @type {Record<string, string>} */ ({}));
  let feishuLimit = $state(100);
  let feishuApprovalId = $state('');
  let feishuIntent = $state(/** @type {any} */ (null));
  let feishuImport = $state(/** @type {any} */ (null));
  let feishuStatusForm = $state(/** @type {HTMLFormElement | null} */ (null));
  let feishuPollTimer = /** @type {ReturnType<typeof setTimeout> | undefined} */ (undefined);
  let feishuPollAttempts = 0;
  let feishuPolling = $state(false);

  const mappingJson = $derived(JSON.stringify(mapping));
  const mappedCount = $derived(Object.values(mapping).filter(Boolean).length);
  const feishuMappingJson = $derived(JSON.stringify(feishuMapping));
  const feishuMappedCount = $derived(Object.values(feishuMapping).filter(Boolean).length);
  const feishuStage = $derived(
    feishuImport?.batchId
      ? 'preview'
      : feishuImport?.id
        ? 'queue'
        : feishuIntent
          ? 'approve'
          : 'configure'
  );
  const backHref = $derived(
    data.opportunity ? `/matching?opportunity=${encodeURIComponent(data.opportunity)}` : '/matching'
  );
  const allVisibleCrmSelected = $derived(
    data.crmCandidates.results.length > 0 &&
      data.crmCandidates.results.every((candidate) => selectedCrmIds.includes(candidate.id))
  );

  $effect(() => {
    const queryKey = `${data.source}:${data.entityType}:${data.search}`;
    if (data.source !== 'crm' || queryKey === presentedCrmQuery) return;
    presentedCrmQuery = queryKey;
    selectedCrmIds = [];
    crmIdempotencyKey = globalThis.crypto?.randomUUID?.() || '';
    actionError = '';
  });

  /** @param {'csv'|'crm'|'feishu'|'email'} source @param {string} [entityType] */
  function sourceHref(source, entityType = '') {
    const params = new URLSearchParams();
    if (source === 'crm') params.set('source', 'crm');
    if (source === 'feishu') params.set('source', 'feishu');
    if (source === 'email') params.set('source', 'email');
    if (source === 'crm' && MATCHING_CRM_ENTITY_TYPES.includes(entityType)) {
      params.set('entity_type', entityType);
    }
    if (data.opportunity) params.set('opportunity', data.opportunity);
    const query = params.toString();
    return `/matching/imports/new${query ? `?${query}` : ''}`;
  }

  /** @param {string} batchId */
  function emailBatchHref(batchId) {
    const opportunity = data.opportunity
      ? `?opportunity=${encodeURIComponent(data.opportunity)}`
      : '';
    return `/matching/imports/${batchId}${opportunity}`;
  }

  /** @param {string} id @param {boolean} checked */
  function toggleCrmRecord(id, checked) {
    selectedCrmIds = checked
      ? [...new Set([...selectedCrmIds, id])].slice(0, 500)
      : selectedCrmIds.filter((candidateId) => candidateId !== id);
    actionError = '';
  }

  function toggleAllVisibleCrm() {
    const visibleIds = data.crmCandidates.results.map((candidate) => candidate.id);
    if (allVisibleCrmSelected) {
      selectedCrmIds = selectedCrmIds.filter((id) => !visibleIds.includes(id));
    } else {
      selectedCrmIds = [...new Set([...selectedCrmIds, ...visibleIds])].slice(0, 500);
    }
    actionError = '';
  }

  /** @param {string} message */
  async function showError(message) {
    actionError = message;
    await tick();
    errorElement?.focus();
  }

  /** @param {File} nextFile */
  async function chooseFile(nextFile) {
    actionError = '';
    if (
      !nextFile.name.toLowerCase().endsWith('.csv') ||
      nextFile.size > MATCHING_IMPORT_MAX_BYTES
    ) {
      await showError('Choose a CSV file no larger than 5 MB.');
      return;
    }
    const parsed = parseCsvHeaders(await nextFile.slice(0, 64 * 1024).text());
    if (!parsed.headers) {
      await showError(parsed.error || 'The CSV header could not be read.');
      return;
    }
    file = nextFile;
    headers = parsed.headers;
    mapping = autoMapImportHeaders(headers);
    idempotencyKey = globalThis.crypto?.randomUUID?.() || '';
    step = 'mapping';
  }

  /** @param {Event & { currentTarget: HTMLInputElement }} event */
  function onFileChange(event) {
    const selected = event.currentTarget.files?.[0];
    if (selected) void chooseFile(selected);
  }

  /** @param {DragEvent} event */
  function onDrop(event) {
    event.preventDefault();
    dragOver = false;
    const dropped = event.dataTransfer?.files?.[0];
    if (!dropped) return;
    if (fileInput && typeof DataTransfer !== 'undefined') {
      const transfer = new DataTransfer();
      transfer.items.add(dropped);
      fileInput.files = transfer.files;
    }
    void chooseFile(dropped);
  }

  /** @param {string} target @param {string} header */
  function setMapping(target, header) {
    const next = { ...mapping };
    if (header) next[target] = header;
    else delete next[target];
    mapping = next;
    actionError = '';
  }

  function resetFile() {
    file = null;
    headers = [];
    mapping = {};
    step = 'upload';
    idempotencyKey = '';
    actionError = '';
    if (fileInput) fileInput.value = '';
  }

  /** @param {string} target @param {string} providerField */
  function setFeishuMapping(target, providerField) {
    const next = { ...feishuMapping };
    const normalized = providerField.trim().slice(0, 100);
    if (normalized) next[target] = normalized;
    else delete next[target];
    feishuMapping = next;
    feishuIntent = null;
    feishuImport = null;
    feishuApprovalId = '';
    actionError = '';
  }

  function clearFeishuTimer() {
    if (feishuPollTimer) clearTimeout(feishuPollTimer);
    feishuPollTimer = undefined;
  }

  function scheduleFeishuPoll() {
    clearFeishuTimer();
    if (!feishuImport?.id || feishuPollAttempts >= 60) return;
    feishuPollTimer = setTimeout(() => feishuStatusForm?.requestSubmit(), 2_000);
  }

  /** @param {any} importRequest */
  async function acceptFeishuImportStatus(importRequest) {
    feishuImport = importRequest;
    if (importRequest?.batchId) {
      clearFeishuTimer();
      const opportunity = data.opportunity
        ? `?opportunity=${encodeURIComponent(data.opportunity)}`
        : '';
      await goto(resolve(`/matching/imports/${importRequest.batchId}${opportunity}`));
      return;
    }
    if (['queued', 'reading'].includes(importRequest?.status)) {
      feishuPollAttempts += 1;
      scheduleFeishuPoll();
      return;
    }
    clearFeishuTimer();
    await showError(
      importRequest?.status === 'unknown'
        ? 'The Feishu read outcome is unknown and remains consumed. Resolve it in Channel Safety before attempting another import.'
        : 'The Feishu import did not produce a safe preview batch.'
    );
  }

  onDestroy(clearFeishuTimer);

  function previewEnhance({ cancel }) {
    const validated = validateImportMapping(mapping, headers);
    if (!validated.mapping) {
      void showError(validated.error || 'Review the column mapping.');
      cancel();
      return;
    }
    busy = true;
    actionError = '';
    return async ({ result, update }) => {
      busy = false;
      const actionData = /** @type {any} */ (result).data;
      if (result.type === 'success' && actionData?.batch?.id) {
        const opportunity = data.opportunity
          ? `?opportunity=${encodeURIComponent(data.opportunity)}`
          : '';
        await goto(resolve(`/matching/imports/${actionData.batch.id}${opportunity}`));
        return;
      }
      await update({ reset: false, invalidateAll: false });
      await showError(actionData?.actionError || 'The CSV could not be previewed.');
    };
  }

  function crmPreviewEnhance({ cancel }) {
    const validated = validateCrmImportSelection(data.entityType, selectedCrmIds);
    if (!validated.payload || !crmIdempotencyKey) {
      void showError(validated.error || 'The CRM preview request is not ready yet.');
      cancel();
      return;
    }
    busy = true;
    actionError = '';
    return async ({ result, update }) => {
      busy = false;
      const actionData = /** @type {any} */ (result).data;
      if (result.type === 'success' && actionData?.batch?.id) {
        const opportunity = data.opportunity
          ? `?opportunity=${encodeURIComponent(data.opportunity)}`
          : '';
        await goto(resolve(`/matching/imports/${actionData.batch.id}${opportunity}`));
        return;
      }
      await update({ reset: false, invalidateAll: false });
      await showError(actionData?.actionError || 'The CRM records could not be previewed.');
    };
  }

  function feishuPrepareEnhance({ cancel }) {
    const validated = validateFeishuPersonImportMapping(feishuMapping);
    if (!validated.mapping || feishuLimit < 1 || feishuLimit > 500) {
      void showError(validated.error || 'Choose a record limit between 1 and 500.');
      cancel();
      return;
    }
    busy = true;
    actionError = '';
    return async ({ result, update }) => {
      busy = false;
      const actionData = /** @type {any} */ (result).data;
      if (result.type === 'success' && actionData?.feishuIntent) {
        feishuIntent = actionData.feishuIntent;
        feishuImport = null;
        feishuApprovalId = '';
        return;
      }
      await update({ reset: false, invalidateAll: false });
      await showError(
        actionData?.actionError || 'The Feishu approval intent could not be prepared.'
      );
    };
  }

  function feishuExecuteEnhance({ cancel }) {
    if (!feishuIntent || !feishuApprovalId) {
      void showError('Enter the approval ID issued by Channel Safety.');
      cancel();
      return;
    }
    busy = true;
    actionError = '';
    return async ({ result, update }) => {
      busy = false;
      const actionData = /** @type {any} */ (result).data;
      if (result.type === 'success' && actionData?.feishuImport?.id) {
        feishuApprovalId = '';
        feishuPollAttempts = 0;
        await acceptFeishuImportStatus(actionData.feishuImport);
        return;
      }
      await update({ reset: false, invalidateAll: false });
      await showError(actionData?.actionError || 'The approved Feishu import could not be queued.');
    };
  }

  function feishuStatusEnhance() {
    feishuPolling = true;
    return async ({ result }) => {
      feishuPolling = false;
      const actionData = /** @type {any} */ (result).data;
      if (result.type === 'success' && actionData?.feishuImport?.id === feishuImport?.id) {
        await acceptFeishuImportStatus(actionData.feishuImport);
        return;
      }
      clearFeishuTimer();
      await showError(actionData?.actionError || 'The Feishu import status could not be loaded.');
    };
  }
</script>

<svelte:head><title>Import people · Matching</title></svelte:head>

<div class="flex min-h-0 flex-1 flex-col">
  <PageHeader
    title="Import people"
    subtitle={data.source === 'crm'
      ? 'Select existing CRM records to create traceable people and evidence.'
      : data.source === 'feishu'
        ? 'Read a bounded Feishu Base selection only after an exact Channel Safety approval.'
        : data.source === 'email'
          ? 'Review privacy-minimized inbound Email previews before creating or merging people.'
          : 'Map a CSV into traceable people and evidence before creating any Person records.'}
    breadcrumb={[{ label: 'Matching', href: backHref }, { label: 'Import people' }]}
  >
    {#snippet actions()}
      <Button href={backHref} variant="outline" size="sm">
        <ArrowLeft class="size-3.5" />Back to matching
      </Button>
    {/snippet}
  </PageHeader>

  <main class="mx-auto w-full max-w-5xl flex-1 space-y-5 px-5 py-5 md:px-8">
    <section
      aria-labelledby="matching-import-source-title"
      class="rounded-xl border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-4"
    >
      <h2 id="matching-import-source-title" class="text-sm font-semibold text-[color:var(--text)]">
        Import source
      </h2>
      <div class="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <a
          href={sourceHref('csv')}
          aria-current={data.source === 'csv' ? 'page' : undefined}
          class="flex items-start gap-3 rounded-lg border p-4 outline-none focus-visible:ring-2 focus-visible:ring-[color:var(--ring)] {data.source ===
          'csv'
            ? 'border-[color:var(--violet)] bg-[color:var(--violet-soft)]'
            : 'border-[color:var(--border-faint)] hover:bg-[color:var(--bg-subtle)]'}"
        >
          <FileSpreadsheet class="mt-0.5 size-4 shrink-0" aria-hidden="true" />
          <span>
            <span class="block text-sm font-semibold">CSV file</span>
            <span class="mt-1 block text-xs text-[color:var(--text-muted)]">
              Upload and map an external people list.
            </span>
          </span>
        </a>
        <a
          href={sourceHref('crm', data.entityType)}
          aria-current={data.source === 'crm' ? 'page' : undefined}
          class="flex items-start gap-3 rounded-lg border p-4 outline-none focus-visible:ring-2 focus-visible:ring-[color:var(--ring)] {data.source ===
          'crm'
            ? 'border-[color:var(--violet)] bg-[color:var(--violet-soft)]'
            : 'border-[color:var(--border-faint)] hover:bg-[color:var(--bg-subtle)]'}"
        >
          <Users class="mt-0.5 size-4 shrink-0" aria-hidden="true" />
          <span>
            <span class="block text-sm font-semibold">Internal CRM</span>
            <span class="mt-1 block text-xs text-[color:var(--text-muted)]">
              Select Leads or Contacts already stored in this organization.
            </span>
          </span>
        </a>
        {#if data.permissions.calibrate}
          <a
            href={sourceHref('feishu')}
            aria-current={data.source === 'feishu' ? 'page' : undefined}
            class="flex items-start gap-3 rounded-lg border p-4 outline-none focus-visible:ring-2 focus-visible:ring-[color:var(--ring)] {data.source ===
            'feishu'
              ? 'border-[color:var(--violet)] bg-[color:var(--violet-soft)]'
              : 'border-[color:var(--border-faint)] hover:bg-[color:var(--bg-subtle)]'}"
          >
            <Database class="mt-0.5 size-4 shrink-0" aria-hidden="true" />
            <span>
              <span class="block text-sm font-semibold">Feishu Base</span>
              <span class="mt-1 block text-xs text-[color:var(--text-muted)]">
                Prepare, approve, and import a bounded record set.
              </span>
            </span>
          </a>
          <a
            href={sourceHref('email')}
            aria-current={data.source === 'email' ? 'page' : undefined}
            class="flex items-start gap-3 rounded-lg border p-4 outline-none focus-visible:ring-2 focus-visible:ring-[color:var(--ring)] {data.source ===
            'email'
              ? 'border-[color:var(--violet)] bg-[color:var(--violet-soft)]'
              : 'border-[color:var(--border-faint)] hover:bg-[color:var(--bg-subtle)]'}"
          >
            <Mail class="mt-0.5 size-4 shrink-0" aria-hidden="true" />
            <span>
              <span class="block text-sm font-semibold">Inbound Email</span>
              <span class="mt-1 block text-xs text-[color:var(--text-muted)]">
                Review safe previews from an SDR-routed mailbox.
              </span>
            </span>
          </a>
        {/if}
      </div>
    </section>

    <div class="hidden sm:block">
      <StageStepper
        stages={data.source === 'crm'
          ? crmStages
          : data.source === 'feishu'
            ? feishuStages
            : data.source === 'email'
              ? emailStages
              : csvStages}
        current={data.source === 'crm'
          ? 'select'
          : data.source === 'feishu'
            ? feishuStage
            : data.source === 'email'
              ? 'preview'
              : step}
      />
    </div>
    <p class="text-xs font-medium text-[color:var(--text-muted)] sm:hidden">
      {#if data.source === 'crm'}
        Step 1 of 5 · Select CRM records
      {:else if data.source === 'feishu'}
        Step {feishuStage === 'configure'
          ? '1'
          : feishuStage === 'approve'
            ? '2'
            : feishuStage === 'queue'
              ? '3'
              : '4'} of 5 · {feishuStage === 'configure'
          ? 'Configure one-time mapping'
          : feishuStage === 'approve'
            ? 'Approve exact intent'
            : feishuStage === 'queue'
              ? 'Build safe preview'
              : 'Open preview batch'}
      {:else if data.source === 'email'}
        Step 2 of 5 · Review safe previews
      {:else}
        Step {step === 'upload' ? '1' : '2'} of 6 · {step === 'upload'
          ? 'Upload CSV'
          : 'Map columns'}
      {/if}
    </p>

    {#if data.source === 'csv'}
      <form
        method="POST"
        action="?/preview"
        enctype="multipart/form-data"
        use:enhance={previewEnhance}
      >
        <input type="hidden" name="mapping" value={mappingJson} />
        <input type="hidden" name="idempotency_key" value={idempotencyKey} />

        <section
          class="rounded-xl border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-5 md:p-6"
        >
          <div class="mb-5 flex items-start gap-3">
            <div
              class="flex size-9 shrink-0 items-center justify-center rounded-lg bg-[color:var(--violet-soft)] text-[color:var(--violet-soft-text)]"
            >
              <FileSpreadsheet class="size-4" aria-hidden="true" />
            </div>
            <div>
              <h2 class="text-base font-semibold text-[color:var(--text)]">
                {step === 'upload' ? 'Choose a CSV file' : 'Map CSV columns'}
              </h2>
              <p class="mt-1 text-sm text-[color:var(--text-muted)]">
                {step === 'upload'
                  ? 'UTF-8 CSV only, no more than 5 MB or 500 data rows.'
                  : 'Display name and at least one identity channel are required.'}
              </p>
            </div>
          </div>

          <input
            bind:this={fileInput}
            id="matching-import-file"
            name="file"
            type="file"
            accept=".csv,text/csv"
            class="sr-only"
            onchange={onFileChange}
          />

          {#if step === 'upload'}
            <button
              type="button"
              class="flex min-h-44 w-full flex-col items-center justify-center gap-3 rounded-xl border-2 border-dashed px-5 text-center transition-colors focus-visible:ring-2 focus-visible:ring-[color:var(--ring)] focus-visible:outline-none {dragOver
                ? 'border-[color:var(--violet)] bg-[color:var(--violet-soft)]'
                : 'border-[color:var(--border)] bg-[color:var(--bg)] hover:bg-[color:var(--bg-subtle)]'}"
              ondragover={(event) => {
                event.preventDefault();
                dragOver = true;
              }}
              ondragleave={() => (dragOver = false)}
              ondrop={onDrop}
              onclick={() => fileInput?.click()}
            >
              <Upload class="size-7 text-[color:var(--text-muted)]" aria-hidden="true" />
              <span class="text-sm font-medium text-[color:var(--text)]"
                >Drop a CSV here or choose a file</span
              >
              <span class="text-xs text-[color:var(--text-subtle)]"
                >The source is fixed to Manual and cannot be mapped.</span
              >
            </button>
          {:else}
            <div
              class="mb-5 flex flex-wrap items-center gap-3 rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg)] px-4 py-3"
            >
              <FileSpreadsheet class="size-4 text-[color:var(--text-muted)]" aria-hidden="true" />
              <div class="min-w-0 flex-1">
                <p class="truncate text-sm font-medium text-[color:var(--text)]">{file?.name}</p>
                <p class="text-xs text-[color:var(--text-subtle)]">
                  {headers.length} columns · {mappedCount} mapped
                </p>
              </div>
              <Button type="button" size="sm" variant="outline" onclick={resetFile}
                >Replace file</Button
              >
            </div>

            <div class="space-y-6">
              {#each fieldGroups as group (group)}
                <fieldset>
                  <legend class="mb-2 text-sm font-semibold text-[color:var(--text)]"
                    >{group}</legend
                  >
                  <div class="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                    {#each MATCHING_IMPORT_FIELDS.filter((field) => field.group === group) as field (field.target)}
                      <label
                        class="grid gap-1.5 text-xs font-medium text-[color:var(--text-muted)]"
                      >
                        <span>
                          {field.label}
                          {#if field.required}<span class="text-[color:var(--red)]"> *</span>{/if}
                        </span>
                        <select
                          class="h-9 w-full rounded-md border border-[color:var(--border)] bg-[color:var(--bg)] px-2.5 text-sm text-[color:var(--text)] outline-none focus-visible:ring-2 focus-visible:ring-[color:var(--ring)]"
                          value={mapping[field.target] || ''}
                          onchange={(event) => setMapping(field.target, event.currentTarget.value)}
                        >
                          <option value="">Not mapped</option>
                          {#each headers as header (header)}
                            <option value={header}>{header}</option>
                          {/each}
                        </select>
                      </label>
                    {/each}
                  </div>
                  {#if group === 'Identity'}
                    <p class="mt-2 text-xs text-[color:var(--text-subtle)]">
                      Map at least one channel. Identity values are masked in server previews and
                      batch history.
                    </p>
                  {/if}
                </fieldset>
              {/each}
            </div>
          {/if}

          {#if actionError}
            <div
              bind:this={errorElement}
              tabindex="-1"
              role="alert"
              class="mt-5 rounded-lg border border-[color:var(--red)] bg-[color:var(--red-soft)] px-4 py-3 text-sm text-[color:var(--red-soft-text)] outline-none"
            >
              {actionError}
            </div>
          {/if}

          {#if step === 'mapping'}
            <div
              class="mt-6 flex flex-wrap justify-end gap-2 border-t border-[color:var(--border-faint)] pt-4"
            >
              <Button type="button" variant="outline" disabled={busy} onclick={resetFile}
                >Back</Button
              >
              <Button type="submit" disabled={busy} aria-busy={busy}>
                {#if busy}<Loader2 class="size-3.5 animate-spin motion-reduce:animate-none" />{/if}
                {busy ? 'Creating preview…' : 'Create safe preview'}
              </Button>
            </div>
          {/if}
        </section>
      </form>
    {:else if data.source === 'crm'}
      <section
        class="rounded-xl border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-5 md:p-6"
        aria-labelledby="crm-import-title"
      >
        <div class="flex flex-wrap items-start justify-between gap-4">
          <div>
            <h2 id="crm-import-title" class="text-base font-semibold text-[color:var(--text)]">
              Select CRM records
            </h2>
            <p class="mt-1 max-w-2xl text-sm text-[color:var(--text-muted)]">
              Selected records are normalized into shared Person and Evidence records. Importing a
              record does not grant consent or permission to contact that person.
            </p>
          </div>
          <span
            class="rounded-full bg-[color:var(--bg-subtle)] px-3 py-1 text-xs font-medium tabular-nums"
          >
            {selectedCrmIds.length} selected
          </span>
        </div>

        <div class="mt-5 flex flex-wrap gap-2" aria-label="CRM entity type">
          {#each MATCHING_CRM_ENTITY_TYPES as entityType (entityType)}
            <Button
              href={sourceHref('crm', entityType)}
              size="sm"
              variant={data.entityType === entityType ? 'default' : 'outline'}
              aria-current={data.entityType === entityType ? 'page' : undefined}
            >
              {entityType === 'lead' ? 'Leads' : 'Contacts'}
            </Button>
          {/each}
        </div>

        <form method="GET" class="mt-4 flex flex-col gap-2 sm:flex-row" role="search">
          <input type="hidden" name="source" value="crm" />
          <input type="hidden" name="entity_type" value={data.entityType} />
          {#if data.opportunity}
            <input type="hidden" name="opportunity" value={data.opportunity} />
          {/if}
          <label for="crm-import-search" class="sr-only">Search CRM records</label>
          <div class="relative min-w-0 flex-1">
            <Search
              class="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-[color:var(--text-subtle)]"
              aria-hidden="true"
            />
            <input
              id="crm-import-search"
              name="search"
              value={data.search}
              maxlength="100"
              placeholder={`Search ${data.entityType === 'lead' ? 'leads' : 'contacts'}…`}
              class="h-9 w-full rounded-md border border-[color:var(--border)] bg-[color:var(--bg)] pr-3 pl-9 text-sm outline-none focus-visible:ring-2 focus-visible:ring-[color:var(--ring)]"
            />
          </div>
          <Button type="submit" size="sm" variant="outline">Search</Button>
        </form>

        {#if data.crmCandidates.results.length > 0}
          <div class="mt-5 overflow-hidden rounded-lg border border-[color:var(--border-faint)]">
            <div
              class="flex items-center justify-between gap-3 border-b border-[color:var(--border-faint)] bg-[color:var(--bg-subtle)] px-4 py-3"
            >
              <label class="flex items-center gap-2 text-xs font-medium">
                <input
                  type="checkbox"
                  checked={allVisibleCrmSelected}
                  onchange={toggleAllVisibleCrm}
                />
                Select all shown
              </label>
              <span class="text-xs text-[color:var(--text-subtle)]">
                Showing {data.crmCandidates.results.length} of {data.crmCandidates.count}
              </span>
            </div>
            <ul class="divide-y divide-[color:var(--border-faint)]">
              {#each data.crmCandidates.results as candidate (candidate.id)}
                <li>
                  <label
                    class="flex cursor-pointer items-start gap-3 px-4 py-4 hover:bg-[color:var(--bg-subtle)]"
                  >
                    <input
                      type="checkbox"
                      class="mt-1"
                      checked={selectedCrmIds.includes(candidate.id)}
                      onchange={(event) =>
                        toggleCrmRecord(candidate.id, event.currentTarget.checked)}
                    />
                    <span class="min-w-0 flex-1">
                      <span class="block text-sm font-semibold text-[color:var(--text)]">
                        {candidate.displayName}
                      </span>
                      <span class="mt-1 block text-xs text-[color:var(--text-muted)]">
                        {[candidate.currentTitle, candidate.currentCompany]
                          .filter(Boolean)
                          .join(' · ') || 'No role summary'}
                      </span>
                      {#if candidate.maskedIdentities.length > 0}
                        <span class="mt-2 flex flex-wrap gap-2">
                          {#each candidate.maskedIdentities as identity, index (`${identity.kind}:${identity.maskedValue}:${index}`)}
                            <span
                              class="rounded-full border border-[color:var(--border-faint)] px-2 py-0.5 text-[11px] text-[color:var(--text-subtle)]"
                            >
                              {identity.kind || 'identity'} · {identity.maskedValue}
                            </span>
                          {/each}
                        </span>
                      {/if}
                    </span>
                  </label>
                </li>
              {/each}
            </ul>
          </div>
        {:else}
          <div
            class="mt-5 rounded-lg border border-dashed border-[color:var(--border)] p-8 text-center"
          >
            <p class="text-sm font-medium text-[color:var(--text)]">No matching CRM records</p>
            <p class="mt-1 text-xs text-[color:var(--text-muted)]">
              Change the entity type or search term.
            </p>
          </div>
        {/if}

        {#if actionError}
          <div
            bind:this={errorElement}
            tabindex="-1"
            role="alert"
            class="mt-5 rounded-lg border border-[color:var(--red)] bg-[color:var(--red-soft)] px-4 py-3 text-sm text-[color:var(--red-soft-text)] outline-none"
          >
            {actionError}
          </div>
        {/if}

        <form
          method="POST"
          action="?/crmPreview"
          use:enhance={crmPreviewEnhance}
          class="mt-6 flex flex-wrap items-center justify-between gap-3 border-t border-[color:var(--border-faint)] pt-4"
        >
          <input type="hidden" name="entity_type" value={data.entityType} />
          <input type="hidden" name="idempotency_key" value={crmIdempotencyKey} />
          {#each selectedCrmIds as recordId (recordId)}
            <input type="hidden" name="record_ids" value={recordId} />
          {/each}
          <p class="text-xs text-[color:var(--text-subtle)]">
            Preview creates a temporary review batch, but no Person or Evidence until you commit.
          </p>
          <Button
            type="submit"
            disabled={busy || selectedCrmIds.length === 0 || !crmIdempotencyKey}
            aria-busy={busy}
          >
            {#if busy}<Loader2 class="size-3.5 animate-spin motion-reduce:animate-none" />{/if}
            {busy ? 'Creating preview…' : 'Create safe preview'}
          </Button>
        </form>
      </section>
    {:else if data.source === 'email'}
      <section
        class="rounded-xl border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-5 md:p-6"
        aria-labelledby="email-import-title"
      >
        <div class="flex flex-wrap items-start justify-between gap-4">
          <div>
            <h2 id="email-import-title" class="text-base font-semibold text-[color:var(--text)]">
              Inbound Email review queue
            </h2>
            <p class="mt-1 max-w-2xl text-sm text-[color:var(--text-muted)]">
              An SDR-routed SES mailbox creates one temporary preview for each accepted, unknown
              correspondent. Reply and opt-out messages only stop or suppress sending; they do not
              become new people.
            </p>
          </div>
          <span class="rounded-full bg-[color:var(--bg-subtle)] px-3 py-1 text-xs font-medium">
            Admin only · review required
          </span>
        </div>

        <div
          class="mt-5 rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-subtle)] p-4"
        >
          <p class="text-sm font-semibold text-[color:var(--text)]">Privacy boundary</p>
          <ul class="mt-2 list-disc space-y-1 pl-5 text-xs text-[color:var(--text-muted)]">
            <li>
              Only sender identity, display name, receipt time, and fixed evidence text enter
              preview staging.
            </li>
            <li>
              Subject, body, HTML, recipients, headers, attachments, and raw Message-ID are not
              shown.
            </li>
            <li>
              An inbound message is evidence of an interaction, not consent for marketing or
              recruiting.
            </li>
            <li>
              Nothing creates or merges a Person until an administrator explicitly commits the
              batch.
            </li>
          </ul>
        </div>

        {#if data.emailLoadError}
          <div
            class="mt-5 rounded-lg border border-[color:var(--red)] bg-[color:var(--red-soft)] px-4 py-3 text-sm text-[color:var(--red-soft-text)]"
          >
            {data.emailLoadError}
          </div>
        {:else if data.emailBatches.results.length > 0}
          <div class="mt-5 overflow-hidden rounded-lg border border-[color:var(--border-faint)]">
            <div
              class="flex items-center justify-between gap-3 border-b border-[color:var(--border-faint)] bg-[color:var(--bg-subtle)] px-4 py-3"
            >
              <p class="text-xs font-semibold text-[color:var(--text)]">Latest safe previews</p>
              <span class="text-xs text-[color:var(--text-subtle)]">
                Showing {data.emailBatches.results.length} of {data.emailBatches.count}
              </span>
            </div>
            <ul class="divide-y divide-[color:var(--border-faint)]">
              {#each data.emailBatches.results as batch (batch.id)}
                <li>
                  <a
                    href={emailBatchHref(batch.id)}
                    class="flex items-center justify-between gap-4 px-4 py-4 hover:bg-[color:var(--bg-subtle)] focus-visible:ring-2 focus-visible:ring-[color:var(--ring)] focus-visible:outline-none"
                  >
                    <span class="min-w-0">
                      <span class="block text-sm font-semibold text-[color:var(--text)]">
                        {batch.fileName}
                      </span>
                      <span class="mt-1 block text-xs text-[color:var(--text-muted)]">
                        {batch.counts.total} correspondent · {batch.counts.ready} ready · no address exposed
                      </span>
                    </span>
                    <span
                      class="shrink-0 rounded-full border border-[color:var(--border-faint)] px-2.5 py-1 text-xs font-medium text-[color:var(--text-muted)]"
                      >{batch.status || 'previewed'}</span
                    >
                  </a>
                </li>
              {/each}
            </ul>
          </div>
        {:else}
          <div
            class="mt-5 rounded-lg border border-dashed border-[color:var(--border)] p-8 text-center"
          >
            <Mail class="mx-auto size-7 text-[color:var(--text-subtle)]" aria-hidden="true" />
            <p class="mt-2 text-sm font-medium text-[color:var(--text)]">No Email previews yet</p>
            <p class="mt-1 text-xs text-[color:var(--text-muted)]">
              Configure an inbound mailbox with the SDR route. This screen never reads an external
              mailbox on its own.
            </p>
            <a
              href={resolve('/settings/inbound-email')}
              class="mt-3 inline-flex text-xs font-semibold underline underline-offset-2"
              >Open inbound Email settings</a
            >
          </div>
        {/if}
      </section>
    {:else}
      <section
        class="rounded-xl border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-5 md:p-6"
        aria-labelledby="feishu-import-title"
      >
        <div class="flex flex-wrap items-start justify-between gap-4">
          <div>
            <h2 id="feishu-import-title" class="text-base font-semibold text-[color:var(--text)]">
              Import from Feishu Base
            </h2>
            <p class="mt-1 max-w-2xl text-sm text-[color:var(--text-muted)]">
              Field names stay in this page and the encrypted import request. Record values are not
              returned until the shared preview masks identities and records safe validation errors.
            </p>
          </div>
          <span class="rounded-full bg-[color:var(--bg-subtle)] px-3 py-1 text-xs font-medium">
            Admin only · approval required
          </span>
        </div>

        {#if data.feishuLoadError}
          <div
            class="mt-5 rounded-lg border border-[color:var(--red)] bg-[color:var(--red-soft)] px-4 py-3 text-sm text-[color:var(--red-soft-text)]"
          >
            {data.feishuLoadError}
          </div>
        {:else if !data.feishuConnection.configured || !data.feishuConnection.active}
          <div
            class="mt-5 rounded-lg border border-amber-300 bg-amber-50 px-4 py-4 text-sm text-amber-950"
          >
            <p class="font-semibold">Feishu Base is not ready for imports.</p>
            <p class="mt-1 text-xs">
              Save the app credentials and Base target, then enable the connection. No provider read
              occurs while saving the configuration.
            </p>
            <a
              href={resolve('/settings/automation')}
              class="mt-3 inline-flex text-xs font-semibold underline underline-offset-2"
              >Open automation settings</a
            >
          </div>
        {:else}
          <form
            method="POST"
            action="?/feishuPrepare"
            use:enhance={feishuPrepareEnhance}
            class="mt-6"
          >
            <input type="hidden" name="mapping" value={feishuMappingJson} />
            <div class="space-y-6">
              {#each feishuFieldGroups as group (group)}
                <fieldset>
                  <legend class="mb-2 text-sm font-semibold text-[color:var(--text)]"
                    >{group}</legend
                  >
                  <div class="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                    {#each FEISHU_PERSON_IMPORT_FIELDS.filter((field) => field.group === group) as field (field.target)}
                      <label
                        class="grid gap-1.5 text-xs font-medium text-[color:var(--text-muted)]"
                      >
                        <span>{field.label}</span>
                        <input
                          value={feishuMapping[field.target] || ''}
                          maxlength="100"
                          autocomplete="off"
                          placeholder="Exact Base field name"
                          class="h-9 w-full rounded-md border border-[color:var(--border)] bg-[color:var(--bg)] px-2.5 text-sm text-[color:var(--text)] outline-none focus-visible:ring-2 focus-visible:ring-[color:var(--ring)]"
                          oninput={(event) =>
                            setFeishuMapping(field.target, event.currentTarget.value)}
                        />
                      </label>
                    {/each}
                  </div>
                  {#if group === 'Person'}
                    <p class="mt-2 text-xs text-[color:var(--text-subtle)]">
                      Map display name, or at least one of first name and last name.
                    </p>
                  {:else if group === 'Identity'}
                    <p class="mt-2 text-xs text-[color:var(--text-subtle)]">
                      Map email, phone, or LinkedIn. Remote record IDs cannot be mapped.
                    </p>
                  {/if}
                </fieldset>
              {/each}
            </div>

            <div
              class="mt-5 flex flex-wrap items-end justify-between gap-4 border-t border-[color:var(--border-faint)] pt-4"
            >
              <label class="grid gap-1.5 text-xs font-medium text-[color:var(--text-muted)]">
                <span>Maximum records</span>
                <input
                  name="limit"
                  type="number"
                  min="1"
                  max="500"
                  step="1"
                  bind:value={feishuLimit}
                  class="h-9 w-32 rounded-md border border-[color:var(--border)] bg-[color:var(--bg)] px-2.5 text-sm text-[color:var(--text)] outline-none focus-visible:ring-2 focus-visible:ring-[color:var(--ring)]"
                />
              </label>
              <div class="text-right">
                <p class="mb-2 text-xs text-[color:var(--text-subtle)]">
                  {feishuMappedCount} mapped fields · preparing the intent performs no provider read.
                </p>
                <Button type="submit" disabled={busy || Boolean(feishuImport?.id)} aria-busy={busy}>
                  {#if busy}<Loader2
                      class="size-3.5 animate-spin motion-reduce:animate-none"
                    />{/if}
                  {busy
                    ? 'Preparing…'
                    : feishuIntent
                      ? 'Prepare a new intent'
                      : 'Prepare approval intent'}
                </Button>
              </div>
            </div>
          </form>

          {#if feishuIntent && !feishuImport?.id}
            <div
              class="mt-6 rounded-xl border border-[color:var(--violet)] bg-[color:var(--violet-soft)] p-4"
            >
              <h3 class="text-sm font-semibold text-[color:var(--violet-soft-text)]">
                Exact approval required
              </h3>
              <p class="mt-1 text-xs text-[color:var(--text-muted)]">
                Open Channel Safety in a new tab, add the internal target if needed, and issue an
                approval with the exact action, payload hash, and unit count below.
              </p>
              <dl class="mt-4 grid gap-3 text-xs sm:grid-cols-2">
                <div>
                  <dt class="text-[color:var(--text-subtle)]">Action</dt>
                  <dd class="mt-1 font-mono break-all">{feishuIntent.action}</dd>
                </div>
                <div>
                  <dt class="text-[color:var(--text-subtle)]">Units</dt>
                  <dd class="mt-1 font-mono">{feishuIntent.units}</dd>
                </div>
                <div class="sm:col-span-2">
                  <dt class="text-[color:var(--text-subtle)]">Internal test target</dt>
                  <dd class="mt-1 font-mono break-all">{feishuIntent.testTargetIdentifier}</dd>
                </div>
                <div class="sm:col-span-2">
                  <dt class="text-[color:var(--text-subtle)]">Target hash</dt>
                  <dd class="mt-1 font-mono break-all">{feishuIntent.targetHash}</dd>
                </div>
                <div class="sm:col-span-2">
                  <dt class="text-[color:var(--text-subtle)]">Payload hash</dt>
                  <dd class="mt-1 font-mono break-all">{feishuIntent.payloadHash}</dd>
                </div>
              </dl>
              <a
                href={resolve('/settings/channel-safety')}
                target="_blank"
                rel="noopener noreferrer"
                class="mt-4 inline-flex text-xs font-semibold underline underline-offset-2"
                >Open Channel Safety in a new tab</a
              >

              <form
                method="POST"
                action="?/feishuExecute"
                use:enhance={feishuExecuteEnhance}
                class="mt-4 flex flex-col gap-3 border-t border-[color:var(--border-faint)] pt-4 sm:flex-row sm:items-end"
              >
                <input type="hidden" name="mapping" value={feishuMappingJson} />
                <input type="hidden" name="limit" value={feishuLimit} />
                <label
                  class="grid min-w-0 flex-1 gap-1.5 text-xs font-medium text-[color:var(--text-muted)]"
                >
                  <span>Approval ID</span>
                  <input
                    name="approval_id"
                    bind:value={feishuApprovalId}
                    maxlength="36"
                    autocomplete="off"
                    placeholder="00000000-0000-4000-8000-000000000000"
                    class="h-9 w-full rounded-md border border-[color:var(--border)] bg-[color:var(--bg)] px-2.5 font-mono text-sm text-[color:var(--text)] outline-none focus-visible:ring-2 focus-visible:ring-[color:var(--ring)]"
                  />
                </label>
                <Button type="submit" disabled={busy || !feishuApprovalId} aria-busy={busy}>
                  {#if busy}<Loader2
                      class="size-3.5 animate-spin motion-reduce:animate-none"
                    />{/if}
                  Queue approved import
                </Button>
              </form>
            </div>
          {/if}

          {#if feishuImport?.id}
            <div
              class="mt-6 rounded-xl border border-[color:var(--border-faint)] bg-[color:var(--bg-subtle)] p-4"
            >
              <div class="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <h3 class="text-sm font-semibold text-[color:var(--text)]">
                    Building safe preview
                  </h3>
                  <p class="mt-1 text-xs text-[color:var(--text-muted)]">
                    Status: {feishuImport.status}. This page polls only the internal ledger and will
                    open the shared batch preview when it is ready.
                  </p>
                </div>
                {#if feishuPolling}
                  <Loader2
                    class="size-4 animate-spin text-[color:var(--text-muted)] motion-reduce:animate-none"
                  />
                {/if}
              </div>
              <dl class="mt-4 grid grid-cols-3 gap-3 text-center text-xs">
                <div class="rounded-lg bg-[color:var(--bg)] p-3">
                  <dt class="text-[color:var(--text-subtle)]">Total</dt>
                  <dd class="mt-1 text-lg font-semibold tabular-nums">{feishuImport.totalCount}</dd>
                </div>
                <div class="rounded-lg bg-[color:var(--bg)] p-3">
                  <dt class="text-[color:var(--text-subtle)]">Ready</dt>
                  <dd class="mt-1 text-lg font-semibold tabular-nums">{feishuImport.readyCount}</dd>
                </div>
                <div class="rounded-lg bg-[color:var(--bg)] p-3">
                  <dt class="text-[color:var(--text-subtle)]">Invalid</dt>
                  <dd class="mt-1 text-lg font-semibold tabular-nums">
                    {feishuImport.invalidCount}
                  </dd>
                </div>
              </dl>
              {#if feishuImport.status === 'unknown'}
                <a
                  href={resolve('/settings/channel-safety')}
                  target="_blank"
                  rel="noopener noreferrer"
                  class="mt-4 inline-flex text-xs font-semibold underline underline-offset-2"
                  >Open Channel Safety to reconcile this consumed request</a
                >
              {/if}
              <form
                bind:this={feishuStatusForm}
                method="POST"
                action="?/feishuStatus"
                use:enhance={feishuStatusEnhance}
                class="mt-4 flex justify-end"
              >
                <input type="hidden" name="import_id" value={feishuImport.id} />
                <Button type="submit" size="sm" variant="outline" disabled={feishuPolling}>
                  {feishuPolling ? 'Checking…' : 'Check status now'}
                </Button>
              </form>
            </div>
          {/if}

          {#if actionError}
            <div
              bind:this={errorElement}
              tabindex="-1"
              role="alert"
              class="mt-5 rounded-lg border border-[color:var(--red)] bg-[color:var(--red-soft)] px-4 py-3 text-sm text-[color:var(--red-soft-text)] outline-none"
            >
              {actionError}
            </div>
          {/if}
        {/if}
      </section>
    {/if}
  </main>
</div>
