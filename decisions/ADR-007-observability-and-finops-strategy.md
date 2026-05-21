# ADR-007: Observability strategy with custom metrics and FinOps-driven log exclusion

- **Status:** Accepted
- **Date:** 2025-04-25
- **Deciders:** Jahaziel Carballo (Solo Developer)

---

## Context

Axima runs on GPU-accelerated Cloud VMs (Spot instances) whose cost can spiral if not carefully monitored and controlled. Beyond infrastructure cost, the platform must deliver reliable performance to multiple tenants, requiring visibility into queue latency, GPU utilization, and error rates.

Google Cloud provides built-in metrics (CPU, memory, disk), but operational decisions for a SaaS product require application-level signals: how long documents wait in queue, how efficiently GPU memory is used, and whether any tenant is experiencing starvation.

Additionally, Cloud Logging charges by ingestion volume. In a busy worker processing thousands of documents, health-check pings, debug logs, and repetitive heartbeat messages generate noise that inflates costs without adding operational value.

I needed an observability strategy that:

1. Exposes application-level metrics critical for autoscaling and alerting.
2. Controls logging costs by excluding low-value noise.
3. Integrates seamlessly with GCP's native tools without adding external dependencies.

---

## Decision

**Use Cloud Monitoring custom metrics emitted by workers via an internal batching thread, combined with project-level log exclusion filters to suppress noise, and compute schedules aligned with business hours for cost efficiency.**

### Implementation details

1. **Custom metrics pipeline:**
   - Workers emit metrics (`queue_latency_seconds`, `vram_usage_gb`, `throughput_ppm`, `errors_total`) via a non-blocking, bounded `multiprocessing.Queue` (`METRIC_QUEUE_GLOBAL`, max 5000 entries).
   - A dedicated `CloudMonitoringBatcher` thread drains this queue every 60 seconds and writes time series to Cloud Monitoring via `google-cloud-monitoring`.
   - Batches are chunked to 200 series per API call (Cloud Monitoring's limit), with retry on failure.
   - On graceful shutdown, the batcher flushes all remaining metrics before the process exits (see ADR-002).

2. **Autoscaling driven by custom metrics:**
   - The Spot MIG autoscaler targets `custom.googleapis.com/axima/ocr/queue_latency_seconds` (see ADR-002).
   - The On-Demand MIG uses the same metric with a lower threshold for emergency scale-out.

3. **Log exclusion filters (FinOps):**
   - Health-check requests are excluded to avoid paying for load balancer pings.
   - Debug-level logs and verbose heartbeat events are filtered out at the project level using `google_logging_project_exclusion` resources managed by Terraform.
   - Default log retention is reduced from 400 days to 30 days via `google_logging_bucket_config`.

4. **Business-hours compute scheduling:**
   - The Spot MIG autoscaler includes scaling schedules that ensure at least 1 replica from 07:55 to 20:05 (Mexico City time), Monday–Saturday, matching accounting firms' working hours.
   - Saturday shutdown at 16:00 forces zero replicas, avoiding weekend GPU costs.

5. **Health endpoint with deep diagnostics:**
   - The `/health` endpoint reports not just "alive" but VRAM usage, open file descriptors, internal queue saturation, and upload executor backlog.
   - This enables precise readiness probes and operational debugging without SSH access.

---

## Alternatives considered

| Alternative | Rejected because |
|-------------|------------------|
| **Prometheus + Grafana (self-hosted)** | Adds operational overhead of maintaining TSDB and dashboards. Cloud Monitoring is natively integrated and requires zero maintenance. |
| **No custom metrics, rely only on GCP built-in** | Built-in metrics (CPU, memory) do not reflect application-level health: a worker can have low CPU but high queue latency due to GPU saturation. |
| **Streaming logs to BigQuery for analysis** | Overkill for current scale; adds cost and complexity. Log exclusion is simpler and sufficient. |
| **External APM (Datadog, New Relic)** | Excellent tools, but add significant cost and a second vendor to manage. GCP-native observability meets current needs. |
| **Keep all logs indefinitely** | Prohibitively expensive. 30-day retention covers debugging while respecting budget. |

---

## Consequences

### Positive

- **Autoscaling reflects real user experience:** Queue latency directly measures how long users wait, not a proxy like CPU.
- **Noisy logs are suppressed at source:** Project-level exclusion filters prevent noisy logs from ever being ingested, saving costs immediately.
- **Zero-maintenance monitoring:** Cloud Monitoring is fully managed. No Prometheus instances to patch.
- **Business-aligned compute:** Scheduling VMs to match actual usage saves ~30% of GPU costs without impacting users.
- **Deep health checks:** Enable precise debugging and readiness decisions.

### Negative

- **Metric delivery latency:** The 60-second batching interval adds up to 60s of staleness. Acceptable for autoscaling (which has its own cooldown), but real-time dashboards would need a push-based approach.
- **Log exclusion risk:** Over-aggressive filters could hide valuable debug information. Mitigated by careful filter design and the ability to temporarily disable filters during incidents.
- **Vendor lock-in:** Custom metrics are GCP-specific. Migrating to another cloud would require rebuilding the metrics pipeline.
- **Single monitoring tool:** Relying solely on Cloud Monitoring means no second opinion during outages. Mitigated by external health checks and alerting redundancy.

---

## References

- Google Cloud Monitoring custom metrics: https://cloud.google.com/monitoring/custom-metrics
- Google Cloud Logging exclusion filters: https://cloud.google.com/logging/docs/exclusions
- GCP Compute Engine autoscaling with custom metrics: https://cloud.google.com/compute/docs/autoscaler/scaling-cloud-monitoring-metrics