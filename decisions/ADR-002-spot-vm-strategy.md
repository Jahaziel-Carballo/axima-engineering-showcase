# ADR-002: Spot VM strategy with on-demand fallback for GPU workers

- **Status:** Accepted
- **Date:** 2025-02-20
- **Deciders:** Jahaziel Carballo (Solo Developer)

---

## Context

Axima's document processing workers require NVIDIA L4 GPUs, which are among the most expensive resources on Google Cloud. Running these workers 24/7 on standard (on-demand) VMs would make the unit economics unsustainable for a bootstrapped SaaS product.

Google offers **Spot VMs** at a 60–70% discount, but with a critical trade-off: GCP can terminate them at any time with only a 30-second preemption notice. A processing worker caught mid-inference during preemption would lose the document result, waste GPU cycles, and potentially leave Firestore in an inconsistent state.

I needed a strategy that captures the cost savings of Spot VMs while meeting two non-negotiable requirements:

1. **No data loss:** A preempted document must be safely returned to the queue for another worker to process.
2. **Predictable latency:** If Spot capacity becomes unavailable (e.g., during cloud-wide shortages), document processing should not stall indefinitely.

---

## Decision

**Use a dual-MIG architecture with queue-latency-driven autoscaling and a 25-second hard-deadline graceful shutdown.**

### Architecture

1. **Primary MIG (Spot):** A regional managed instance group of `g2-standard-12` VMs with L4 GPUs, provisioned as Spot instances. This MIG handles all normal workloads.

2. **Emergency MIG (On-Demand):** A second, independent MIG with identical hardware but provisioned as standard on-demand instances. It scales to 0 by default and only activates when the Spot MIG cannot keep up.

3. **Custom autoscaling metric:** Both MIGs are driven by `custom.googleapis.com/axima/ocr/queue_latency_seconds`, a metric emitted by the workers themselves. This metric measures the age of the oldest unprocessed message in Pub/Sub, capped at 300 seconds to avoid distortion by outliers.

4. **Different thresholds for each MIG:**
   - **Spot MIG:** Scales up when queue latency exceeds 120 seconds. Scales down after 300 seconds of cooldown.
   - **On-Demand MIG:** Scales up only when queue latency exceeds 45 seconds (same SLA target). Scales down with a longer 300-second cooldown to avoid costly oscillations.

5. **Business-hours scheduling:** The Spot MIG has a scaling schedule that ensures at least 1 replica from 07:55 to 20:05 (Mexico City time), Monday–Saturday, matching when accounting firms use the platform.

### Graceful shutdown for Spot preemption

The worker's `graceful_shutdown()` function is triggered by a SIGTERM handler and executes a strict sequence within a 25-second hard deadline (leaving 5 seconds of margin within the 30-second GCP notice):

1. **Stop ingress:** Cancel Pub/Sub streaming pull, stop the dispatcher thread.
2. **Drain buffers:** NACK all queued but unprocessed tasks so they return to Pub/Sub immediately.
3. **Wait for in-flight tasks:** Up to 10 seconds for currently processing child workers to finish and write results to `/dev/shm`.
4. **Drain upload executor:** Flush all pending Cloud Storage uploads.
5. **Stop children:** Send stop events to child processes, join with 5-second timeout, force-terminate if necessary.
6. **Flush metrics:** Send any remaining custom metrics to Cloud Monitoring.
7. **VRAM cleanup:** Explicitly call `cuda.empty_cache()` and `gc.collect()`.

If any step hangs, a `threading.Timer(25.0, hard_exit)` fires `os._exit(0)` to guarantee the VM terminates cleanly without corrupting external state.

---

## Alternatives considered

| Alternative | Rejected because |
|-------------|------------------|
| **On-Demand only** | Prohibitively expensive for a bootstrapped product: ~$700/month per GPU vs. ~$200/month with Spot. |
| **Spot only (no fallback)** | During GCP Spot shortages (which can last hours), processing would stall completely, breaking the SLA. |
| **Kubernetes with Spot node pools** | Added complexity (GKE cluster management, GPU node taints/tolerations) without clear benefit over MIGs for a single-workload system. MIGs provide simpler integration with health checks and autoscaling. |
| **Preemptible VMs (older GCP offering)** | Preemptible VMs have a fixed 24-hour maximum lifetime; Spot VMs have no such limit, reducing forced restarts. |
| **Reserved Instances + Spot mix** | Reservations require 1-year or 3-year commitments, which are too rigid for an early-stage product still validating its traffic patterns. |
| **Queue depth as scaling metric** | Queue depth alone doesn't distinguish between a burst of small documents (easy to clear) and a few massive documents (slow to process). Latency better reflects user-perceived delay. |

---

## Consequences

### Positive

- **60–70% reduction in GPU costs** compared to running on-demand exclusively.
- **Zero data loss during preemption:** The multi-layered shutdown ensures every document is either completed and uploaded, or safely returned to Pub/Sub for another worker to handle.
- **Graceful degradation under Spot shortage:** The On-Demand MIG acts as a pressure-release valve, maintaining processing (at higher cost) only when absolutely necessary.
- **Predictable availability:** The scheduling profiles align compute capacity with actual usage patterns, avoiding idle GPUs on weekends.
- **Observable decision-making:** The queue latency metric provides a clear signal that justifies scaling actions to both engineers and stakeholders.

### Negative

- **Increased architectural complexity:** Two MIGs, two autoscalers, and two instance templates must be maintained in sync (e.g., both must be updated when the worker image tag changes).
- **Cold start latency:** The On-Demand MIG takes 5–7 minutes to boot a new VM (golden image pull + Docker container start + model loading). During this window, the Spot MIG must absorb the load alone. Mitigated by the Spot MIG's 120-second latency target, which triggers scaling well before saturation.
- **Shutdown race conditions:** The 25-second hard timer is a sledgehammer. If GCP sends the preemption signal late or the worker is processing an unusually large document, data loss is still theoretically possible. Mitigated by extensive testing and the upload executor's `shutdown(wait=True)` call.
- **Metric dependency:** The autoscaler depends on custom metrics being delivered to Cloud Monitoring. If the metric pipeline breaks (e.g., Cloud Monitoring API quota exhausted), scaling decisions degrade. Mitigated by the On-Demand MIG having the same metric target, so both scale up or down together.

---

## References

- GCP Spot VM documentation: https://cloud.google.com/spot-vms
- GCP Compute Engine preemption notice behavior
- Custom metric autoscaling: https://cloud.google.com/compute/docs/autoscaler/scaling-cloud-monitoring-metrics
- PaddleOCR GPU memory behavior under load (internal testing)