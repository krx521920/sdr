# SDR lead routing

SDR routing converts the normalized country, acquisition source, and
qualification result into an explainable sales assignment. Rules belong to one
organization and are evaluated in ascending priority order.

## Decision flow

```text
normalized lead
    |
    v
active rules ordered by priority
    |
    +-- country matches (empty = any)
    +-- source matches (empty = any)
    +-- qualification band matches (empty = any)
    |
    v
first match with an active Sales-access member
    |
    +-- least_loaded: fewest active CRM leads
    +-- round_robin: transactionally advance the rule cursor
    +-- direct: first configured member
    |
    v
write lead + assignment + rule/reason to the intake ledger
```

If a matching rule has no eligible member, evaluation continues. If no rule can
produce an assignment, the existing organization-wide least-loaded router is
used. This fail-open behavior prevents an incomplete administrator configuration
from dropping an inbound lead.

Only active profiles with `has_sales_access` participate in a configured sales
pool. Rule members retain an explicit position so direct and round-robin routing
are deterministic. A routing preview reads the current round-robin cursor but
does not advance it or create a CRM record.

## Tenant isolation

The following tables carry `org_id` and use PostgreSQL row-level security:

- `sdr_routing_rule`
- `sdr_routing_rule_member`
- `sdr_routing_rule_state`
- `sdr_lead_intake`, including its matched rule and human-readable reason

The API also filters rule and profile queries by the authenticated organization.
A profile from another tenant cannot be added to a rule.

## Administration

Organization administrators manage rules at `/settings/sdr-routing` or through:

```text
GET  /api/sdr/routing-rules/
POST /api/sdr/routing-rules/
GET  /api/sdr/routing-rules/<rule-id>/
PUT  /api/sdr/routing-rules/<rule-id>/
PATCH /api/sdr/routing-rules/<rule-id>/
DELETE /api/sdr/routing-rules/<rule-id>/
POST /api/sdr/routing-rules/preview/
```

Deployments must apply Django migration `sdr.0002_sdr_routing_rules` before the
new router receives production traffic.
