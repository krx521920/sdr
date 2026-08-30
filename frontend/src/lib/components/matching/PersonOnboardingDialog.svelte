<script>
  import { tick } from 'svelte';
  import { AlertCircle, ArrowLeft, ArrowRight, Check, Loader2, ShieldCheck } from '@lucide/svelte';

  import { Button } from '$lib/components/ui/button/index.js';
  import * as Dialog from '$lib/components/ui/dialog/index.js';
  import { Input } from '$lib/components/ui/input/index.js';
  import { StageStepper } from '$lib/components/ui/stage-stepper/index.js';
  import { Textarea } from '$lib/components/ui/textarea/index.js';
  import {
    buildOnboardingEvidence,
    buildOnboardingIdentity,
    buildOnboardingPerson,
    buildPersonOnboardingPayload,
    maskIdentityValue
  } from '$lib/matching/onboarding.js';

  const steps = [
    { value: 'person', label: 'Person', meta: 'Profile' },
    { value: 'identity', label: 'Identity', meta: 'Channel' },
    { value: 'evidence', label: 'Evidence', meta: 'Provenance' },
    { value: 'review', label: 'Review', meta: 'Confirm' }
  ];

  const availabilityOptions = [
    { value: 'unknown', label: 'Unknown' },
    { value: 'available', label: 'Available' },
    { value: 'open_to_offers', label: 'Open to offers' },
    { value: 'busy', label: 'Busy' },
    { value: 'unavailable', label: 'Unavailable' }
  ];

  const identityOptions = [
    { value: 'email', label: 'Email' },
    { value: 'phone', label: 'Phone' },
    { value: 'linkedin', label: 'LinkedIn' },
    { value: 'whatsapp', label: 'WhatsApp' },
    { value: 'wechat', label: 'WeChat' },
    { value: 'external', label: 'External ID' }
  ];

  const evidenceOptions = [
    { value: 'profile', label: 'Profile' },
    { value: 'skill', label: 'Skill' },
    { value: 'experience', label: 'Experience' },
    { value: 'relationship', label: 'Relationship' },
    { value: 'interaction', label: 'Interaction' },
    { value: 'availability', label: 'Availability' },
    { value: 'preference', label: 'Preference' },
    { value: 'verification', label: 'Verification' },
    { value: 'other', label: 'Other' }
  ];

  /**
   * @type {{
   *   open?: boolean,
   *   busy?: boolean,
   *   error?: string,
   *   onSubmit?: (payload: Record<string, unknown>) => void | Promise<void>,
   *   onRequestClose?: (context: { dirty: boolean, close: () => void }) => void,
   *   onOpenChange?: (open: boolean) => void,
   *   onClearError?: () => void,
   *   onStepChange?: (step: string) => void
   * }}
   */
  let {
    open = $bindable(false),
    busy = false,
    error = '',
    onSubmit,
    onRequestClose,
    onOpenChange,
    onClearError,
    onStepChange
  } = $props();

  let stepIndex = $state(0);
  let submitting = $state(false);
  let internalError = $state('');
  let errorField = $state('');
  let errorElement = $state(/** @type {HTMLElement | null} */ (null));
  let stepHeadingElement = $state(/** @type {HTMLElement | null} */ (null));
  let presentedOpen = $state(false);
  let wasOpen = false;

  let displayName = $state('');
  let currentTitle = $state('');
  let currentCompany = $state('');
  let location = $state('');
  let availability = $state('unknown');
  let skills = $state('');
  let roles = $state('');

  let identityKind = $state('email');
  let identityValue = $state('');

  let evidenceKind = $state('profile');
  let evidenceSummary = $state('');
  let evidenceSourceUri = $state('');
  let evidenceObservedAt = $state('');
  let evidenceValidUntil = $state('');
  let evidenceConfidence = $state('0.5');
  let evidenceSkills = $state('');
  let evidenceTitles = $state('');
  let evidenceLocations = $state('');
  let evidenceAvailability = $state('');

  const currentStep = $derived(steps[stepIndex]?.value || 'person');
  const visibleError = $derived(internalError || error);
  const isBusy = $derived(Boolean(busy || submitting));
  const maskedIdentity = $derived(maskIdentityValue(identityKind, identityValue));
  const dirty = $derived(
    stepIndex > 0 ||
      Boolean(
        displayName ||
        currentTitle ||
        currentCompany ||
        location ||
        skills ||
        roles ||
        identityValue ||
        evidenceSummary ||
        evidenceSourceUri ||
        evidenceValidUntil ||
        evidenceSkills ||
        evidenceTitles ||
        evidenceLocations ||
        evidenceAvailability
      )
  );

  function localDateTimeNow() {
    const date = new Date();
    const offset = date.getTimezoneOffset() * 60_000;
    return new Date(date.getTime() - offset).toISOString().slice(0, 16);
  }

  function resetForm() {
    stepIndex = 0;
    submitting = false;
    internalError = '';
    errorField = '';
    displayName = '';
    currentTitle = '';
    currentCompany = '';
    location = '';
    availability = 'unknown';
    skills = '';
    roles = '';
    identityKind = 'email';
    identityValue = '';
    evidenceKind = 'profile';
    evidenceSummary = '';
    evidenceSourceUri = '';
    evidenceObservedAt = localDateTimeNow();
    evidenceValidUntil = '';
    evidenceConfidence = '0.5';
    evidenceSkills = '';
    evidenceTitles = '';
    evidenceLocations = '';
    evidenceAvailability = '';
    onClearError?.();
    onStepChange?.('person');
  }

  $effect(() => {
    if (open && !wasOpen) resetForm();
    presentedOpen = open;
    wasOpen = open;
  });

  $effect(() => {
    const message = visibleError;
    if (open && message) {
      tick().then(() => errorElement?.focus());
    }
  });

  function personInput() {
    return {
      displayName,
      currentTitle,
      currentCompany,
      location,
      availability,
      skills,
      roles
    };
  }

  function identityInput() {
    return { kind: identityKind, value: identityValue };
  }

  function evidenceInput() {
    return {
      kind: evidenceKind,
      summary: evidenceSummary,
      sourceUri: evidenceSourceUri,
      observedAt: evidenceObservedAt,
      validUntil: evidenceValidUntil,
      confidence: evidenceConfidence,
      skills: evidenceSkills,
      titles: evidenceTitles,
      locations: evidenceLocations,
      availability: evidenceAvailability
    };
  }

  /** @param {{ error?: string, field?: string }} result */
  function applyBuilderError(result) {
    internalError = result.error || 'Review the highlighted onboarding field.';
    errorField = result.field || '';
  }

  function clearError() {
    internalError = '';
    errorField = '';
    onClearError?.();
  }

  async function focusStepHeading() {
    await tick();
    stepHeadingElement?.focus();
  }

  async function nextStep() {
    if (isBusy) return;
    let result;
    if (currentStep === 'person') result = buildOnboardingPerson(personInput());
    else if (currentStep === 'identity') result = buildOnboardingIdentity(identityInput());
    else if (currentStep === 'evidence') result = buildOnboardingEvidence(evidenceInput());
    else return;

    if (!result.payload) {
      applyBuilderError(result);
      return;
    }
    clearError();
    stepIndex = Math.min(stepIndex + 1, steps.length - 1);
    onStepChange?.(currentStep);
    await focusStepHeading();
  }

  async function previousStep() {
    if (isBusy || stepIndex === 0) return;
    clearError();
    stepIndex -= 1;
    onStepChange?.(currentStep);
    await focusStepHeading();
  }

  function closeNow() {
    presentedOpen = false;
    open = false;
    onOpenChange?.(false);
  }

  function requestClose() {
    if (isBusy) return;
    if (dirty && onRequestClose) {
      onRequestClose({ dirty: true, close: closeNow });
      return;
    }
    closeNow();
  }

  /** @param {boolean} nextOpen */
  function handleOpenChange(nextOpen) {
    if (nextOpen) {
      presentedOpen = true;
      open = true;
      onOpenChange?.(true);
    } else {
      // Keep the controlled dialog mounted while the parent asks whether a
      // dirty draft should be discarded. This prevents parent and primitive
      // open state from diverging when the operator chooses Keep editing.
      presentedOpen = true;
      requestClose();
    }
  }

  /** @param {SubmitEvent} event */
  async function handleSubmit(event) {
    event.preventDefault();
    if (currentStep !== 'review') {
      await nextStep();
      return;
    }
    if (isBusy) return;

    const result = buildPersonOnboardingPayload({
      person: personInput(),
      identity: identityInput(),
      evidence: evidenceInput()
    });
    if (!result.payload) {
      applyBuilderError(result);
      return;
    }
    if (!onSubmit) {
      applyBuilderError({ error: 'Person onboarding is not available right now.' });
      return;
    }

    clearError();
    submitting = true;
    try {
      // The component intentionally ignores callback results so an API response
      // containing identity or evidence internals cannot enter component state.
      await onSubmit(result.payload);
    } catch {
      applyBuilderError({ error: 'The person could not be saved. Your form is still available.' });
    } finally {
      submitting = false;
    }
  }

  /** @param {string} value */
  function label(value) {
    return value
      .split('_')
      .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
      .join(' ');
  }

  /** @param {string} value */
  function termCount(value) {
    return new Set(
      value
        .split(/[,;\n]/)
        .map((part) => part.trim().toLowerCase())
        .filter(Boolean)
    ).size;
  }

  /** @param {string} field */
  function ariaInvalid(field) {
    return visibleError && errorField === field ? true : undefined;
  }

  /** @param {string} field */
  function ariaDescribedBy(field) {
    return visibleError && errorField === field ? 'person-onboarding-error' : undefined;
  }
</script>

<Dialog.Root bind:open={presentedOpen} onOpenChange={handleOpenChange}>
  <Dialog.Content class="max-h-[92vh] overflow-y-auto sm:max-w-2xl">
    <Dialog.Header>
      <Dialog.Title>Add a person</Dialog.Title>
      <Dialog.Description>
        Add one channel identity and one source-attributed evidence record. Manual provenance is
        applied automatically.
      </Dialog.Description>
    </Dialog.Header>

    <StageStepper stages={steps} current={currentStep} />

    <form
      class="space-y-5"
      aria-busy={isBusy}
      aria-describedby={visibleError ? 'person-onboarding-error' : undefined}
      onsubmit={handleSubmit}
    >
      <p class="sr-only" aria-live="polite">
        {isBusy
          ? 'Saving person onboarding.'
          : `Step ${stepIndex + 1} of ${steps.length}: ${label(currentStep)}`}
      </p>

      {#if visibleError}
        <div
          id="person-onboarding-error"
          bind:this={errorElement}
          class="flex items-start gap-2 rounded-lg border border-[color:var(--red)] bg-[color:var(--red-soft)] px-3 py-2 text-sm text-[color:var(--red)] outline-none"
          role="alert"
          tabindex="-1"
        >
          <AlertCircle class="mt-0.5 size-4 shrink-0" aria-hidden="true" />
          <span>{visibleError}</span>
        </div>
      {/if}

      <section aria-labelledby="person-onboarding-step-title">
        <h3
          id="person-onboarding-step-title"
          bind:this={stepHeadingElement}
          class="mb-4 text-sm font-semibold text-[color:var(--text)] outline-none"
          tabindex="-1"
        >
          {#if currentStep === 'person'}Person profile
          {:else if currentStep === 'identity'}Primary identity
          {:else if currentStep === 'evidence'}Traceable evidence
          {:else}Review before saving{/if}
        </h3>

        {#if currentStep === 'person'}
          <div class="space-y-4">
            <div class="space-y-1.5">
              <label for="onboarding-display-name" class="text-xs font-medium">
                Name <span class="text-[color:var(--red)]">(required)</span>
              </label>
              <Input
                id="onboarding-display-name"
                bind:value={displayName}
                maxlength={255}
                required
                autocomplete="name"
                placeholder="Alice Chen"
                aria-invalid={ariaInvalid('displayName')}
                aria-describedby={ariaDescribedBy('displayName')}
              />
            </div>
            <div class="grid gap-4 sm:grid-cols-2">
              <div class="space-y-1.5">
                <label for="onboarding-current-title" class="text-xs font-medium"
                  >Current title</label
                >
                <Input
                  id="onboarding-current-title"
                  bind:value={currentTitle}
                  maxlength={255}
                  autocomplete="organization-title"
                  placeholder="Growth engineer"
                  aria-invalid={ariaInvalid('currentTitle')}
                  aria-describedby={ariaDescribedBy('currentTitle')}
                />
              </div>
              <div class="space-y-1.5">
                <label for="onboarding-current-company" class="text-xs font-medium"
                  >Current company</label
                >
                <Input
                  id="onboarding-current-company"
                  bind:value={currentCompany}
                  maxlength={255}
                  autocomplete="organization"
                  placeholder="Acme"
                  aria-invalid={ariaInvalid('currentCompany')}
                  aria-describedby={ariaDescribedBy('currentCompany')}
                />
              </div>
            </div>
            <div class="grid gap-4 sm:grid-cols-2">
              <div class="space-y-1.5">
                <label for="onboarding-location" class="text-xs font-medium">Location</label>
                <Input
                  id="onboarding-location"
                  bind:value={location}
                  maxlength={255}
                  autocomplete="address-level2"
                  placeholder="Shanghai"
                  aria-invalid={ariaInvalid('location')}
                  aria-describedby={ariaDescribedBy('location')}
                />
              </div>
              <div class="space-y-1.5">
                <label for="onboarding-availability" class="text-xs font-medium">Availability</label
                >
                <select
                  id="onboarding-availability"
                  bind:value={availability}
                  aria-invalid={ariaInvalid('availability')}
                  aria-describedby={ariaDescribedBy('availability')}
                  class="h-9 w-full rounded-[var(--r-md)] border border-[color:var(--border)] bg-[color:var(--bg-input)] px-3 text-[13px]"
                >
                  {#each availabilityOptions as option (option.value)}
                    <option value={option.value}>{option.label}</option>
                  {/each}
                </select>
              </div>
            </div>
            <div class="grid gap-4 sm:grid-cols-2">
              <div class="space-y-1.5">
                <label for="onboarding-skills" class="text-xs font-medium">Skills</label>
                <Textarea
                  id="onboarding-skills"
                  bind:value={skills}
                  maxlength={4000}
                  rows={3}
                  placeholder="Python, outbound automation"
                  aria-invalid={ariaInvalid('skills')}
                  aria-describedby={ariaDescribedBy('skills')}
                />
              </div>
              <div class="space-y-1.5">
                <label for="onboarding-roles" class="text-xs font-medium">Roles</label>
                <Textarea
                  id="onboarding-roles"
                  bind:value={roles}
                  maxlength={4000}
                  rows={3}
                  placeholder="Growth engineer, SDR specialist"
                  aria-invalid={ariaInvalid('roles')}
                  aria-describedby={ariaDescribedBy('roles')}
                />
              </div>
            </div>
            <p class="text-xs text-[color:var(--text-subtle)]">
              Separate skills and roles with commas or new lines.
            </p>
          </div>
        {:else if currentStep === 'identity'}
          <div class="space-y-4">
            <div class="grid gap-4 sm:grid-cols-[180px_1fr]">
              <div class="space-y-1.5">
                <label for="onboarding-identity-kind" class="text-xs font-medium">
                  Identity type <span class="text-[color:var(--red)]">(required)</span>
                </label>
                <select
                  id="onboarding-identity-kind"
                  bind:value={identityKind}
                  aria-invalid={ariaInvalid('identityKind')}
                  aria-describedby={ariaDescribedBy('identityKind')}
                  class="h-9 w-full rounded-[var(--r-md)] border border-[color:var(--border)] bg-[color:var(--bg-input)] px-3 text-[13px]"
                >
                  {#each identityOptions as option (option.value)}
                    <option value={option.value}>{option.label}</option>
                  {/each}
                </select>
              </div>
              <div class="space-y-1.5">
                <label for="onboarding-identity-value" class="text-xs font-medium">
                  Identity value <span class="text-[color:var(--red)]">(required)</span>
                </label>
                <Input
                  id="onboarding-identity-value"
                  type={identityKind === 'email'
                    ? 'email'
                    : identityKind === 'phone' || identityKind === 'whatsapp'
                      ? 'tel'
                      : 'text'}
                  bind:value={identityValue}
                  maxlength={500}
                  required
                  autocomplete={identityKind === 'email'
                    ? 'email'
                    : identityKind === 'phone' || identityKind === 'whatsapp'
                      ? 'tel'
                      : 'off'}
                  placeholder={identityKind === 'email'
                    ? 'alice@example.com'
                    : 'Channel identifier'}
                  aria-invalid={ariaInvalid('identityValue')}
                  aria-describedby={ariaDescribedBy('identityValue')}
                />
              </div>
            </div>
            <div
              class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-3 text-xs text-[color:var(--text-muted)]"
            >
              This will be stored as the primary identity with manual provenance. The Review step
              only shows a masked value.
            </div>
          </div>
        {:else if currentStep === 'evidence'}
          <div class="space-y-4">
            <div class="grid gap-4 sm:grid-cols-[180px_1fr]">
              <div class="space-y-1.5">
                <label for="onboarding-evidence-kind" class="text-xs font-medium">
                  Evidence type <span class="text-[color:var(--red)]">(required)</span>
                </label>
                <select
                  id="onboarding-evidence-kind"
                  bind:value={evidenceKind}
                  aria-invalid={ariaInvalid('evidenceKind')}
                  aria-describedby={ariaDescribedBy('evidenceKind')}
                  class="h-9 w-full rounded-[var(--r-md)] border border-[color:var(--border)] bg-[color:var(--bg-input)] px-3 text-[13px]"
                >
                  {#each evidenceOptions as option (option.value)}
                    <option value={option.value}>{option.label}</option>
                  {/each}
                </select>
              </div>
              <div class="space-y-1.5">
                <label for="onboarding-confidence" class="text-xs font-medium">Confidence</label>
                <select
                  id="onboarding-confidence"
                  bind:value={evidenceConfidence}
                  aria-invalid={ariaInvalid('confidence')}
                  aria-describedby={ariaDescribedBy('confidence')}
                  class="h-9 w-full rounded-[var(--r-md)] border border-[color:var(--border)] bg-[color:var(--bg-input)] px-3 text-[13px]"
                >
                  <option value="0.3">Low</option>
                  <option value="0.5">Medium</option>
                  <option value="0.8">High</option>
                  <option value="1">Verified</option>
                </select>
              </div>
            </div>
            <div class="space-y-1.5">
              <label for="onboarding-evidence-summary" class="text-xs font-medium">
                Evidence summary <span class="text-[color:var(--red)]">(required)</span>
              </label>
              <Textarea
                id="onboarding-evidence-summary"
                bind:value={evidenceSummary}
                maxlength={5000}
                rows={4}
                required
                placeholder="Summarize what was observed and why it matters."
                aria-invalid={ariaInvalid('evidenceSummary')}
                aria-describedby={ariaDescribedBy('evidenceSummary')}
              />
            </div>
            <div class="space-y-1.5">
              <label for="onboarding-source-uri" class="text-xs font-medium">Reference link</label>
              <Input
                id="onboarding-source-uri"
                type="url"
                bind:value={evidenceSourceUri}
                maxlength={1000}
                placeholder="https://example.com/profile"
                aria-invalid={ariaInvalid('sourceUri')}
                aria-describedby={ariaDescribedBy('sourceUri')}
              />
            </div>
            <div class="grid gap-4 sm:grid-cols-2">
              <div class="space-y-1.5">
                <label for="onboarding-observed-at" class="text-xs font-medium">
                  Observed at <span class="text-[color:var(--red)]">(required)</span>
                </label>
                <Input
                  id="onboarding-observed-at"
                  type="datetime-local"
                  bind:value={evidenceObservedAt}
                  required
                  aria-invalid={ariaInvalid('observedAt')}
                  aria-describedby={ariaDescribedBy('observedAt')}
                />
              </div>
              <div class="space-y-1.5">
                <label for="onboarding-valid-until" class="text-xs font-medium">Valid until</label>
                <Input
                  id="onboarding-valid-until"
                  type="datetime-local"
                  bind:value={evidenceValidUntil}
                  aria-invalid={ariaInvalid('validUntil')}
                  aria-describedby={ariaDescribedBy('validUntil')}
                />
              </div>
            </div>
            <fieldset class="space-y-3 rounded-lg border border-[color:var(--border-faint)] p-4">
              <legend class="px-1 text-xs font-semibold">Structured facts</legend>
              <p class="text-xs text-[color:var(--text-subtle)]">
                Only these four deterministic matching dimensions are accepted.
              </p>
              <div class="grid gap-4 sm:grid-cols-2">
                <div class="space-y-1.5">
                  <label for="onboarding-evidence-skills" class="text-xs font-medium">Skills</label>
                  <Textarea
                    id="onboarding-evidence-skills"
                    bind:value={evidenceSkills}
                    maxlength={4000}
                    rows={2}
                    placeholder="Python, Django"
                    aria-invalid={ariaInvalid('evidenceSkills')}
                    aria-describedby={ariaDescribedBy('evidenceSkills')}
                  />
                </div>
                <div class="space-y-1.5">
                  <label for="onboarding-evidence-titles" class="text-xs font-medium">Titles</label>
                  <Textarea
                    id="onboarding-evidence-titles"
                    bind:value={evidenceTitles}
                    maxlength={4000}
                    rows={2}
                    placeholder="Growth engineer"
                    aria-invalid={ariaInvalid('evidenceTitles')}
                    aria-describedby={ariaDescribedBy('evidenceTitles')}
                  />
                </div>
                <div class="space-y-1.5">
                  <label for="onboarding-evidence-locations" class="text-xs font-medium"
                    >Locations</label
                  >
                  <Textarea
                    id="onboarding-evidence-locations"
                    bind:value={evidenceLocations}
                    maxlength={4000}
                    rows={2}
                    placeholder="Shanghai, Remote"
                    aria-invalid={ariaInvalid('evidenceLocations')}
                    aria-describedby={ariaDescribedBy('evidenceLocations')}
                  />
                </div>
                <div class="space-y-1.5">
                  <label for="onboarding-evidence-availability" class="text-xs font-medium"
                    >Availability</label
                  >
                  <select
                    id="onboarding-evidence-availability"
                    bind:value={evidenceAvailability}
                    aria-invalid={ariaInvalid('evidenceAvailability')}
                    aria-describedby={ariaDescribedBy('evidenceAvailability')}
                    class="h-9 w-full rounded-[var(--r-md)] border border-[color:var(--border)] bg-[color:var(--bg-input)] px-3 text-[13px]"
                  >
                    <option value="">Not stated</option>
                    {#each availabilityOptions.filter((option) => option.value !== 'unknown') as option (option.value)}
                      <option value={option.value}>{option.label}</option>
                    {/each}
                  </select>
                </div>
              </div>
            </fieldset>
          </div>
        {:else}
          <div class="space-y-4">
            <div class="rounded-lg border border-[color:var(--border-faint)] p-4">
              <dl class="grid gap-3 text-sm sm:grid-cols-[140px_1fr]">
                <dt class="text-[color:var(--text-subtle)]">Person</dt>
                <dd class="font-medium">{displayName}</dd>
                <dt class="text-[color:var(--text-subtle)]">Context</dt>
                <dd>
                  {[currentTitle, currentCompany, location].filter(Boolean).join(' · ') ||
                    'Not provided'}
                </dd>
                <dt class="text-[color:var(--text-subtle)]">Profile facts</dt>
                <dd>
                  {termCount(skills)} skills · {termCount(roles)} roles · {label(availability)}
                </dd>
                <dt class="text-[color:var(--text-subtle)]">Primary identity</dt>
                <dd>{label(identityKind)} · {maskedIdentity}</dd>
                <dt class="text-[color:var(--text-subtle)]">Evidence</dt>
                <dd>
                  {label(evidenceKind)} · manual provenance · {label(
                    evidenceConfidence === '1'
                      ? 'verified'
                      : evidenceConfidence === '0.8'
                        ? 'high'
                        : evidenceConfidence === '0.3'
                          ? 'low'
                          : 'medium'
                  )} confidence
                </dd>
                <dt class="text-[color:var(--text-subtle)]">Structured facts</dt>
                <dd>
                  {termCount(evidenceSkills)} skills · {termCount(evidenceTitles)} titles ·
                  {termCount(evidenceLocations)} locations
                </dd>
                <dt class="text-[color:var(--text-subtle)]">Reference</dt>
                <dd>{evidenceSourceUri ? 'Reference link attached' : 'No reference link'}</dd>
              </dl>
            </div>
            <div
              class="rounded-lg border border-[color:var(--amber)] bg-[color:var(--amber-soft)] p-3 text-xs text-[color:var(--amber-soft-text)]"
            >
              <div class="flex items-start gap-2">
                <ShieldCheck class="mt-0.5 size-4 shrink-0" aria-hidden="true" />
                <p>
                  Evidence is an audit record. Review the summary and structured facts carefully
                  before saving. The identity value is intentionally masked here.
                </p>
              </div>
            </div>
            <div class="rounded-lg bg-[color:var(--bg-elevated)] p-3 text-sm">
              <p class="font-medium">Evidence summary</p>
              <p class="mt-1 whitespace-pre-wrap text-[color:var(--text-muted)]">
                {evidenceSummary}
              </p>
            </div>
          </div>
        {/if}
      </section>

      <Dialog.Footer>
        <div class="flex w-full flex-wrap items-center justify-between gap-2">
          <div>
            {#if stepIndex > 0}
              <Button type="button" variant="ghost" disabled={isBusy} onclick={previousStep}>
                <ArrowLeft class="mr-1 size-4" aria-hidden="true" />Back
              </Button>
            {/if}
          </div>
          <div class="flex items-center gap-2">
            <Button type="button" variant="outline" disabled={isBusy} onclick={requestClose}>
              Cancel
            </Button>
            {#if currentStep === 'review'}
              <Button type="submit" disabled={isBusy}>
                {#if isBusy}
                  <Loader2
                    class="mr-1 size-4 animate-spin motion-reduce:animate-none"
                    aria-hidden="true"
                  />
                  Saving…
                {:else}
                  <Check class="mr-1 size-4" aria-hidden="true" />Save person
                {/if}
              </Button>
            {:else}
              <Button type="submit" disabled={isBusy}>
                Continue<ArrowRight class="ml-1 size-4" aria-hidden="true" />
              </Button>
            {/if}
          </div>
        </div>
      </Dialog.Footer>
    </form>
  </Dialog.Content>
</Dialog.Root>
