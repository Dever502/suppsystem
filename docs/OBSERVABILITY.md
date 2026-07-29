# Observability and incident runbook

Minimal dashboard, alerts and first-response steps for a single-instance deployment.

## Signals

The application exposes Prometheus-compatible metrics on `/metrics` when the Operator API is enabled. Keep the API bound to loopback and scrape it through the same trusted host or monitoring network.

Useful JSON log fields are `event`, `trace_id`, `ticket_id`, `delivery_id`,
`operator_action_id`, `error_kind` and redacted `error_message`. Common URI credentials, bearer
tokens and query-string secrets are redacted.

## Dashboard starter panels

Start with these panels:

| Panel | PromQL |
| --- | --- |
| Delivery queue depth | `support_queue_depth{queue="delivery"}` |
| Notification queue depth | `support_queue_depth{queue="notification"}` |
| Oldest delivery age | `support_queue_oldest_age_seconds{queue="delivery"}` |
| Oldest notification age | `support_queue_oldest_age_seconds{queue="notification"}` |
| Failed jobs | `support_failed_jobs` |
| Attempts in retained jobs | `support_retained_job_attempts` |
| Worker heartbeat age | `support_heartbeat_age_seconds` |
| Remnawave unknown outcomes | `support_panel_unknown` |
| External failures by component | `increase(support_events_total{outcome=~"failed|retry|unknown|unexpected_response|http_5xx|request_error"}[15m])` |
| External request latency average | `rate(support_external_request_duration_seconds_sum[5m]) / rate(support_external_request_duration_seconds_count[5m])` |

High age with low depth usually means a stuck job; high depth with low age means a traffic spike or
slow downstream.

## Alert rules

A starter Prometheus rule file is available at `deploy/prometheus/supportbot-alerts.yml`.

Tune thresholds to traffic. Failed jobs and unknown Remnawave outcomes are usually immediately actionable.

## Runbook

### `/ready` is degraded

1. Identify the degraded component in the response body and logs.
2. Check Compose/container health and database availability.
3. For a stale heartbeat, inspect whether the worker is blocked on Telegram, webhook or database I/O.

### Delivery queue is old or failed

1. Check failed jobs, oldest age and logs by `delivery_id`/`ticket_id`.
2. Look for missing topics, Telegram rate limits or permanent bad requests.
3. If topic recovery failed, confirm that the Forum topic exists. Do not edit the database directly.

### Notification queue is old or failed

1. Check receiver availability, HTTP status, webhook URL and secret.
2. Respect `Retry-After` unless receiver state changed.
3. Before manual replay, confirm receiver-side deduplication by notification ID.

### Remnawave unknown outcome

1. Do not repeat the operator command while durable reconciliation is pending.
2. Find the related `operator_action_id` and reconciliation job in logs or database audit rows.
3. Wait for automatic `completed`, `not_applied` or `inconclusive` classification.
4. Unavailable or malformed reads retry with backoff and become `inconclusive` after 20 failures.
5. For `inconclusive`, independently prove the fate of the original call. An administrator may
   then use `/resolvepanel <operator_action_uuid> applied|not_applied`; otherwise leave the action blocked.
6. Resolution never repeats a mutation. Confirmed `revokelink applied` performs one read-only lookup
   so the durable notification contains the current link.
7. Correlate `panel_action_manually_reconciled` with the actor and action ID in incident notes.

### Suspected secret exposure

1. Preserve restricted audit logs and rotate the affected secret immediately.
2. Check exceptions, URLs, headers and deployment variables for the value.
3. Add a regression test for the leak shape.

## Incident notes

Record timing, impact, relevant IDs, remediation, recovery method and follow-up tests or alerts.
