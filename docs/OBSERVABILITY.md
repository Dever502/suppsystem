# Observability and incident runbook

This guide describes the minimal dashboard, alerts and first-response steps for a single-instance Telegram Support Platform deployment.

## Signals

The application exposes Prometheus-compatible metrics on `/metrics` when the Operator API is enabled. Keep the API bound to loopback and scrape it through the same trusted host or monitoring network.

Useful log fields:

- `event` for the domain or infrastructure event;
- `trace_id` for one Telegram update or API request;
- `ticket_id`, `delivery_id`, `operator_action_id` for entity-level debugging;
- `error_kind` and redacted `error_message` for failure class.

JSON logs redact common secret shapes such as URI credentials, bearer tokens and query-string secret values.

## Dashboard starter panels

Create a small dashboard with these panels before adding custom business views:

| Panel | PromQL |
| --- | --- |
| Delivery queue depth | `support_queue_depth{queue="delivery"}` |
| Notification queue depth | `support_queue_depth{queue="notification"}` |
| Oldest delivery age | `support_queue_oldest_age_seconds{queue="delivery"}` |
| Oldest notification age | `support_queue_oldest_age_seconds{queue="notification"}` |
| Failed jobs | `support_failed_jobs` |
| Worker heartbeat age | `support_heartbeat_age_seconds` |
| Remnawave unknown outcomes | `support_panel_unknown` |
| External failures by component | `increase(support_events_total{outcome=~"failed|retry|unknown|unexpected_response"}[15m])` |
| External request latency average | `rate(support_external_request_duration_seconds_sum[5m]) / rate(support_external_request_duration_seconds_count[5m])` |

Keep queue depth and oldest age on the same dashboard. A small queue with high age usually means a stuck job; a large queue with low age usually means traffic spike or slow downstream.

## Alert rules

A starter Prometheus rule file is available at:

```text
deploy/prometheus/supportbot-alerts.yml
```

Tune thresholds for your traffic before using them as paging alerts. For quiet installations, failed jobs and unknown Remnawave outcomes are usually actionable immediately.

## Runbook

### `/ready` is degraded

1. Check the response body and identify the degraded component.
2. Check recent logs with `event`, `trace_id` and component-specific IDs.
3. Confirm the database is reachable with `sh scripts/production-compose.sh ps` and container health.
4. If a worker heartbeat is stale, inspect whether the process is alive but blocked on Telegram, webhook or database operations.

### Delivery queue is old or failed

1. Check `support_failed_jobs{queue="delivery"}` and `support_queue_oldest_age_seconds{queue="delivery"}`.
2. Search logs by `delivery_id` and `ticket_id`.
3. Look for Telegram errors such as missing topics, rate limits or permanent bad requests.
4. If topic recovery failed, use ticket/topic logs to confirm whether the Forum topic still exists.
5. Do not edit the database directly unless a documented recovery procedure exists.

### Notification queue is old or failed

1. Check receiver availability and HTTP status codes in logs.
2. Verify webhook URL and secret configuration.
3. If the receiver returned `Retry-After`, wait for the scheduled retry unless the receiver state changed.
4. Confirm the receiver deduplicates events by notification ID before replaying anything manually.

### Remnawave unknown outcome

1. Do not repeat the operator command blindly.
2. Open Remnawave and verify the subscription state directly.
3. Find the related `operator_action_id` in logs or database audit rows.
4. Decide whether the mutation completed, did not apply, or needs manual correction.
5. Record the reconciliation result in the incident notes.

### Suspected secret exposure

1. Preserve logs for audit but restrict access immediately.
2. Rotate the affected token or secret even if it appears redacted elsewhere.
3. Check whether the value was present in an exception, URL, request header or deployment variable.
4. Add a regression test for the leak shape before closing the incident.

## Incident notes

For each incident, record:

- start and end time;
- affected components and users;
- relevant `trace_id`, `ticket_id`, `delivery_id` or `operator_action_id`;
- remediation steps;
- whether retry, restore or manual reconciliation was used;
- follow-up test or alert changes.
