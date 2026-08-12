<script>
  import { PageHeader } from '$lib/components/layout';
  import {
    AlertTriangle,
    ArrowDown,
    BarChart3,
    CheckCircle2,
    Clock3,
    Eye,
    Info,
    Mail,
    MessageCircleReply,
    MousePointerClick,
    Sparkles,
    Target,
    TrendingUp,
    Users
  } from '@lucide/svelte';

  /** @type {{ data: any }} */
  let { data } = $props();

  const analytics = $derived(data.analytics || {});
  const kpis = $derived(analytics.kpis || {});
  const funnel = $derived(analytics.funnel || []);
  const sources = $derived((analytics.sources || []).filter((item) => item.received > 0));
  const trend = $derived(analytics.trend || []);
  const engagement = $derived(analytics.engagement || {});
  const responseSla = $derived(analytics.response_sla || {});
  const maxTrend = $derived(
    Math.max(1, ...trend.map((item) => Number(item.received || 0)))
  );
  const maxFunnel = $derived(Math.max(1, Number(funnel[0]?.count || 0)));

  const kpiCards = $derived([
    {
      label: 'Inbound leads',
      value: formatNumber(kpis.received?.value),
      comparison: comparisonText(kpis.received),
      icon: Users,
      tone: 'text-blue-600 bg-blue-50'
    },
    {
      label: 'MQL',
      value: formatNumber(kpis.mql?.value),
      comparison: comparisonText(kpis.mql),
      icon: Target,
      tone: 'text-violet-600 bg-violet-50'
    },
    {
      label: 'MQL rate',
      value: formatPercent(kpis.mql_rate?.value),
      comparison: `${formatPercent(kpis.mql_rate?.previous)} prior period`,
      icon: TrendingUp,
      tone: 'text-emerald-600 bg-emerald-50'
    },
    {
      label: 'SQL',
      value: formatNumber(kpis.sql?.value),
      comparison: comparisonText(kpis.sql),
      icon: Sparkles,
      tone: 'text-amber-600 bg-amber-50'
    },
    {
      label: 'MQL to SQL',
      value: formatPercent(kpis.mql_to_sql_rate?.value),
      comparison: `${formatPercent(kpis.mql_to_sql_rate?.previous)} prior period`,
      icon: BarChart3,
      tone: 'text-cyan-600 bg-cyan-50'
    }
  ]);

  const engagementMetrics = $derived([
    { label: 'Sent', value: formatNumber(engagement.sent), icon: Mail },
    { label: 'Open rate', value: formatPercent(engagement.open_rate), icon: Eye },
    {
      label: 'Click rate',
      value: formatPercent(engagement.click_rate),
      icon: MousePointerClick
    },
    {
      label: 'Reply rate',
      value: formatPercent(engagement.reply_rate),
      icon: MessageCircleReply
    },
    {
      label: 'Positive replies',
      value: formatPercent(engagement.positive_reply_rate),
      icon: CheckCircle2
    }
  ]);

  /** @param {number | null | undefined} value */
  function formatNumber(value) {
    return new Intl.NumberFormat().format(Number(value || 0));
  }

  /** @param {number | null | undefined} value */
  function formatPercent(value) {
    return `${Number(value || 0).toFixed(1)}%`;
  }

  /** @param {any} metric */
  function comparisonText(metric) {
    if (metric?.change_percent === null || metric?.change_percent === undefined) {
      return `${formatNumber(metric?.previous)} prior period`;
    }
    const prefix = metric.change_percent > 0 ? '+' : '';
    return `${prefix}${Number(metric.change_percent).toFixed(1)}% vs prior period`;
  }

  /** @param {number | null | undefined} value */
  function formatSeconds(value) {
    if (value === null || value === undefined) return '-';
    if (value < 60) return `${Math.round(value)}s`;
    if (value < 3600) return `${(value / 60).toFixed(1)}m`;
    return `${(value / 3600).toFixed(1)}h`;
  }

  /** @param {string} level */
  function insightClasses(level) {
    if (level === 'success') return 'border-emerald-200 bg-emerald-50 text-emerald-800';
    if (level === 'warning') return 'border-amber-200 bg-amber-50 text-amber-900';
    return 'border-blue-200 bg-blue-50 text-blue-800';
  }

  /** @param {string} level */
  function insightIcon(level) {
    if (level === 'success') return CheckCircle2;
    if (level === 'warning') return AlertTriangle;
    return Info;
  }
</script>

<svelte:head>
  <title>SDR Growth Analytics - BottleCRM</title>
</svelte:head>

<PageHeader
  title="SDR Growth Analytics"
  subtitle="Find funnel drop-offs, compare acquisition sources, and improve outreach every week"
>
  {#snippet titleIcon()}
    <BarChart3 class="size-4" />
  {/snippet}
  {#snippet actions()}
    <nav
      aria-label="Reporting period"
      class="flex items-center rounded-md border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-1"
    >
      {#each [7, 30, 90] as days}
        <a
          href={`?days=${days}`}
          aria-current={data.selectedDays === days ? 'page' : undefined}
          class="rounded px-2.5 py-1 text-xs font-medium transition-colors {data.selectedDays ===
          days
            ? 'bg-[color:var(--bg-muted)] text-[color:var(--text)] shadow-sm'
            : 'text-[color:var(--text-muted)] hover:text-[color:var(--text)]'}"
        >
          {days}d
        </a>
      {/each}
    </nav>
  {/snippet}
</PageHeader>

<div class="space-y-6 p-6 md:p-8">
  {#if data.loadError}
    <div class="flex gap-3 rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-800">
      <AlertTriangle class="mt-0.5 size-4 shrink-0" />
      <div>
        <p class="font-semibold">Analytics are temporarily unavailable</p>
        <p class="mt-1 text-xs">{data.loadError}</p>
      </div>
    </div>
  {/if}

  <section class="grid gap-4 sm:grid-cols-2 xl:grid-cols-5">
    {#each kpiCards as card (card.label)}
      <div
        class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-4"
      >
        <div class="flex items-center justify-between gap-3">
          <p class="text-xs font-medium text-[color:var(--text-muted)]">{card.label}</p>
          <span class={`flex size-8 items-center justify-center rounded-lg ${card.tone}`}>
            <card.icon class="size-4" />
          </span>
        </div>
        <p class="mt-3 text-2xl font-semibold tabular-nums text-[color:var(--text)]">
          {card.value}
        </p>
        <p class="mt-1 text-[11px] text-[color:var(--text-subtle)]">{card.comparison}</p>
      </div>
    {/each}
  </section>

  <div class="grid gap-6 xl:grid-cols-[minmax(0,1.55fr)_minmax(320px,0.85fr)]">
    <section
      class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-5"
    >
      <div class="flex items-start justify-between gap-4">
        <div>
          <h2 class="text-sm font-semibold text-[color:var(--text)]">Acquisition funnel</h2>
          <p class="mt-1 text-xs text-[color:var(--text-muted)]">
            A progressive cohort from inbound arrival to CRM conversion
          </p>
        </div>
        <span
          class="rounded-full bg-[color:var(--bg-muted)] px-2.5 py-1 text-[11px] font-medium text-[color:var(--text-muted)]"
        >
          Last {data.selectedDays} days
        </span>
      </div>

      {#if funnel.length}
        <div class="mt-6 space-y-4">
          {#each funnel as stage, index (stage.key)}
            <div>
              <div class="mb-1.5 flex items-end justify-between gap-3 text-xs">
                <div class="flex items-center gap-2">
                  <span class="font-medium text-[color:var(--text)]">{stage.label}</span>
                  {#if index > 0}
                    <span class="text-[11px] text-[color:var(--text-subtle)]">
                      {formatPercent(stage.from_previous_rate)} from prior step
                    </span>
                  {/if}
                </div>
                <span class="font-semibold tabular-nums text-[color:var(--text)]">
                  {formatNumber(stage.count)}
                </span>
              </div>
              <div class="h-8 overflow-hidden rounded-md bg-[color:var(--bg-muted)]">
                <div
                  class="flex h-full min-w-0 items-center rounded-md bg-gradient-to-r from-violet-500 to-blue-500 px-3 text-[10px] font-semibold text-white transition-all"
                  style={`width: ${Math.max(stage.count ? 8 : 0, (stage.count / maxFunnel) * 100)}%`}
                >
                  {#if stage.dropoff_count > 0}
                    <span class="truncate">-{formatNumber(stage.dropoff_count)} drop-off</span>
                  {/if}
                </div>
              </div>
              {#if index < funnel.length - 1}
                <div class="ml-3 mt-1 flex h-2 items-center text-[color:var(--text-subtle)]">
                  <ArrowDown class="size-3" />
                </div>
              {/if}
            </div>
          {/each}
        </div>
      {:else}
        <div class="mt-6 rounded-lg bg-[color:var(--bg-muted)] p-8 text-center text-sm text-[color:var(--text-muted)]">
          No funnel data is available for this period.
        </div>
      {/if}
    </section>

    <section
      class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-5"
    >
      <div class="flex items-center gap-2">
        <Sparkles class="size-4 text-violet-500" />
        <h2 class="text-sm font-semibold text-[color:var(--text)]">Next best actions</h2>
      </div>
      <p class="mt-1 text-xs text-[color:var(--text-muted)]">
        Deterministic recommendations from this reporting window
      </p>

      <div class="mt-5 space-y-3">
        {#each analytics.insights || [] as insight (insight.title)}
          {@const Icon = insightIcon(insight.level)}
          <div class={`rounded-lg border p-3 ${insightClasses(insight.level)}`}>
            <div class="flex gap-2.5">
              <Icon class="mt-0.5 size-4 shrink-0" />
              <div class="min-w-0">
                <p class="text-xs font-semibold">{insight.title}</p>
                <p class="mt-1 text-[11px] leading-5 opacity-80">{insight.detail}</p>
                <p class="mt-2 text-[11px] font-medium leading-5">{insight.action}</p>
              </div>
            </div>
          </div>
        {:else}
          <div class="rounded-lg bg-[color:var(--bg-muted)] p-5 text-center text-xs text-[color:var(--text-muted)]">
            More data is needed before recommending an optimization.
          </div>
        {/each}
      </div>
    </section>
  </div>

  <section
    class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-5"
  >
    <div class="flex flex-wrap items-center justify-between gap-3">
      <div>
        <h2 class="text-sm font-semibold text-[color:var(--text)]">Daily lead volume</h2>
        <p class="mt-1 text-xs text-[color:var(--text-muted)]">
          Received, MQL, and SQL cohorts by arrival date
        </p>
      </div>
      <div class="flex items-center gap-4 text-[11px] text-[color:var(--text-muted)]">
        <span class="flex items-center gap-1.5"><span class="size-2 rounded-sm bg-blue-200"></span>Received</span>
        <span class="flex items-center gap-1.5"><span class="size-2 rounded-sm bg-violet-400"></span>MQL</span>
        <span class="flex items-center gap-1.5"><span class="size-2 rounded-sm bg-emerald-500"></span>SQL</span>
      </div>
    </div>

    <div class="mt-6 flex h-44 items-end gap-px border-b border-[color:var(--border-faint)]">
      {#each trend as point (point.date)}
        <div
          class="group relative h-full min-w-[2px] flex-1"
          title={`${point.date}: ${point.received} received, ${point.mql} MQL, ${point.sql} SQL`}
        >
          <div
            class="absolute inset-x-0 bottom-0 min-h-px rounded-t-sm bg-blue-200"
            style={`height: ${(point.received / maxTrend) * 100}%`}
          ></div>
          <div
            class="absolute inset-x-0 bottom-0 min-h-px rounded-t-sm bg-violet-400"
            style={`height: ${(point.mql / maxTrend) * 100}%`}
          ></div>
          <div
            class="absolute inset-x-0 bottom-0 min-h-px rounded-t-sm bg-emerald-500"
            style={`height: ${(point.sql / maxTrend) * 100}%`}
          ></div>
        </div>
      {/each}
    </div>
    {#if trend.length}
      <div class="mt-2 flex justify-between text-[10px] text-[color:var(--text-subtle)]">
        <span>{trend[0]?.date}</span>
        <span>{trend[trend.length - 1]?.date}</span>
      </div>
    {/if}
  </section>

  <section
    class="overflow-hidden rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)]"
  >
    <div class="flex flex-wrap items-center justify-between gap-3 border-b border-[color:var(--border-faint)] p-5">
      <div>
        <h2 class="text-sm font-semibold text-[color:var(--text)]">Source quality</h2>
        <p class="mt-1 text-xs text-[color:var(--text-muted)]">
          Compare volume with downstream quality before reallocating acquisition effort
        </p>
      </div>
    </div>
    {#if sources.length}
      <div class="overflow-x-auto">
        <table class="w-full min-w-[720px] text-left text-xs">
          <thead class="bg-[color:var(--bg-muted)] text-[color:var(--text-muted)]">
            <tr>
              <th class="px-5 py-3 font-medium">Source</th>
              <th class="px-4 py-3 text-right font-medium">Received</th>
              <th class="px-4 py-3 text-right font-medium">MQL</th>
              <th class="px-4 py-3 text-right font-medium">MQL rate</th>
              <th class="px-4 py-3 text-right font-medium">Handoff</th>
              <th class="px-4 py-3 text-right font-medium">SQL</th>
              <th class="px-5 py-3 text-right font-medium">SQL rate</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-[color:var(--border-faint)]">
            {#each sources as source (source.source)}
              <tr class="hover:bg-[color:var(--bg-muted)]/50">
                <td class="px-5 py-3 font-medium text-[color:var(--text)]">{source.label}</td>
                <td class="px-4 py-3 text-right tabular-nums">{formatNumber(source.received)}</td>
                <td class="px-4 py-3 text-right tabular-nums">{formatNumber(source.mql)}</td>
                <td class="px-4 py-3 text-right font-medium tabular-nums text-violet-600">
                  {formatPercent(source.mql_rate)}
                </td>
                <td class="px-4 py-3 text-right tabular-nums">{formatNumber(source.sales_handoff)}</td>
                <td class="px-4 py-3 text-right tabular-nums">{formatNumber(source.sql)}</td>
                <td class="px-5 py-3 text-right font-medium tabular-nums text-emerald-600">
                  {formatPercent(source.sql_rate)}
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {:else}
      <div class="p-8 text-center text-sm text-[color:var(--text-muted)]">
        No source data is available for this period.
      </div>
    {/if}
  </section>

  <div class="grid gap-6 xl:grid-cols-[minmax(0,1.4fr)_minmax(320px,0.8fr)]">
    <section
      class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-5"
    >
      <div>
        <h2 class="text-sm font-semibold text-[color:var(--text)]">Nurture engagement and A/B</h2>
        <p class="mt-1 text-xs text-[color:var(--text-muted)]">
          Outcomes attributed to emails sent during this reporting period
        </p>
      </div>

      <div class="mt-5 grid gap-3 sm:grid-cols-5">
        {#each engagementMetrics as metric (metric.label)}
          <div class="rounded-lg bg-[color:var(--bg-muted)] p-3">
            <div class="flex items-center gap-1.5 text-[11px] text-[color:var(--text-muted)]">
              <metric.icon class="size-3.5" />
              {metric.label}
            </div>
            <p class="mt-2 text-lg font-semibold tabular-nums">{metric.value}</p>
          </div>
        {/each}
      </div>

      <div class="mt-5 grid gap-4 sm:grid-cols-2">
        {#each engagement.variants || [] as variant (variant.variant)}
          <div class="rounded-lg border border-[color:var(--border-faint)] p-4">
            <div class="flex items-center justify-between">
              <span class="flex size-8 items-center justify-center rounded-full bg-violet-100 text-sm font-semibold text-violet-700">
                {variant.variant}
              </span>
              <span class="text-[11px] text-[color:var(--text-muted)]">
                {formatNumber(variant.sent)} sent
              </span>
            </div>
            <dl class="mt-4 grid grid-cols-3 gap-3 text-center">
              <div>
                <dt class="text-[10px] uppercase tracking-wide text-[color:var(--text-subtle)]">Open</dt>
                <dd class="mt-1 text-sm font-semibold tabular-nums">{formatPercent(variant.open_rate)}</dd>
              </div>
              <div>
                <dt class="text-[10px] uppercase tracking-wide text-[color:var(--text-subtle)]">Reply</dt>
                <dd class="mt-1 text-sm font-semibold tabular-nums">{formatPercent(variant.reply_rate)}</dd>
              </div>
              <div>
                <dt class="text-[10px] uppercase tracking-wide text-[color:var(--text-subtle)]">Positive</dt>
                <dd class="mt-1 text-sm font-semibold tabular-nums text-emerald-600">
                  {formatPercent(variant.positive_reply_rate)}
                </dd>
              </div>
            </dl>
          </div>
        {/each}
      </div>
    </section>

    <section
      class="rounded-lg border border-[color:var(--border-faint)] bg-[color:var(--bg-elevated)] p-5"
    >
      <div class="flex items-center gap-2">
        <Clock3 class="size-4 text-blue-500" />
        <h2 class="text-sm font-semibold text-[color:var(--text)]">Email first-response SLA</h2>
      </div>
      <p class="mt-1 text-xs text-[color:var(--text-muted)]">
        Customer acknowledgement latency for this inbound cohort
      </p>
      <div class="mt-6 flex items-end justify-between gap-4">
        <div>
          <p class="text-3xl font-semibold tabular-nums">
            {formatPercent(responseSla.within_sla_rate)}
          </p>
          <p class="mt-1 text-xs text-[color:var(--text-muted)]">within {formatSeconds(responseSla.sla_seconds)}</p>
        </div>
        <div class="text-right text-xs text-[color:var(--text-muted)]">
          <p><span class="font-semibold text-[color:var(--text)]">{formatSeconds(responseSla.median_seconds)}</span> median</p>
          <p class="mt-1"><span class="font-semibold text-[color:var(--text)]">{formatNumber(responseSla.sample_size)}</span> responses</p>
        </div>
      </div>
      <div class="mt-5 h-2 overflow-hidden rounded-full bg-[color:var(--bg-muted)]">
        <div
          class="h-full rounded-full bg-blue-500"
          style={`width: ${Math.min(100, Number(responseSla.within_sla_rate || 0))}%`}
        ></div>
      </div>
      {#if responseSla.breached > 0}
        <p class="mt-3 text-xs text-amber-700">
          {formatNumber(responseSla.breached)} acknowledgements breached the target.
        </p>
      {:else if responseSla.sample_size === 0}
        <p class="mt-3 text-xs text-[color:var(--text-subtle)]">
          No sent acknowledgement samples are available in this period.
        </p>
      {/if}
    </section>
  </div>

  <section class="rounded-lg bg-[color:var(--bg-muted)] p-4 text-[11px] leading-5 text-[color:var(--text-muted)]">
    <p class="font-semibold text-[color:var(--text)]">Metric definitions</p>
    <p class="mt-1"><strong>MQL:</strong> {analytics.definitions?.mql}</p>
    <p><strong>Sales handoff:</strong> {analytics.definitions?.sales_handoff}</p>
    <p><strong>SQL:</strong> {analytics.definitions?.sql}</p>
  </section>
</div>
