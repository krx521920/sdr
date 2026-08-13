# Apollo automatic outbound sources

SDR outbound campaigns can import prospects on a recurring schedule from Apollo. The integration
uses Apollo People Search to find candidate IDs and People Enrichment only for candidates selected
for import.

## Credit safety

- People Search results are deduplicated against Apollo source URLs already present in the campaign
  before enrichment.
- Each source has `max_results_per_sync` (default 25, maximum 100).
- A source cannot run until an administrator explicitly acknowledges enrichment credit usage.
- One API page is processed per run. The next page is persisted, capped to Apollo's 500-page search
  limit, and wraps back to page 1.
- Each successfully enriched prospect is immediately imported through the existing CSV cleaner and
  CRM-wide deduplication path. A retry therefore skips records already persisted by an earlier
  attempt.

## Configuration

1. Open **Settings → SDR Outbound**.
2. Save an Apollo API key and enable the connection. The key is encrypted at rest and is never
   returned by the API.
3. Select a campaign and add an automatic source with at least one people/company filter.
4. Choose the interval and maximum enrichment requests per run, acknowledge credit usage, then
   optionally enable scheduling.
5. Use **Sync now** for an immediate durable job.

Celery Beat checks for due sources every 15 minutes. Jobs are persisted in the automation ledger
before Redis dispatch and use the normal retry/dead-letter lifecycle.

## Environment

```env
APOLLO_API_BASE_URL=https://api.apollo.io/api/v1
APOLLO_API_TIMEOUT=15
```

The base URL is deployment-owned; tenants can configure only credentials and search filters.
