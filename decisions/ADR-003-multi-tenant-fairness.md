
3. **Fair assignment algorithm:**
- **Tenant-level round-robin:** The dispatcher iterates over active tenants in a circular order. It picks the next tenant regardless of how many tasks that tenant has, ensuring every tenant gets a turn.
- **User-level round-robin:** Within the selected tenant, it pops the next user from `user_rr_list`, takes one task from that user's deque, and if the user still has tasks, appends them back to `user_rr_list`. This ensures users within a tenant are served fairly.
- **Next-fit worker assignment:** The selected task is assigned to the first available child process (GPU worker) found by scanning workers circularly from the last used index. If no worker is available, the task is returned to the front of its user's queue and the dispatcher waits briefly.

4. **Backpressure handling:** If the internal task queue is full, new Pub/Sub messages are nacked, relying on Pub/Sub's redelivery mechanism. If the global tenant buffer exceeds 100 tasks, incoming tasks are also nacked to prevent unbounded memory growth.

5. **Reactive waiting:** The dispatcher uses a `threading.Condition` to sleep when no tasks are available. It is woken up by the Pub/Sub callback whenever a new task is enqueued, avoiding wasteful polling.

### Why two levels?

A single-level round-robin across all users (ignoring tenant boundaries) would allow a tenant with many users to dominate. Conversely, a tenant-level round-robin without per-user fairness would let a single heavy user within a firm starve their colleagues. The two-level approach isolates the problem at each boundary.

---

## Alternatives considered

| Alternative | Rejected because |
|-------------|------------------|
| **Single shared FIFO queue** | No fairness guarantees; one tenant can dominate. |
| **Per-tenant Pub/Sub subscriptions** | Explodes operational complexity (each tenant needs its own subscription, worker pool, and scaling policy). Not viable for a bootstrapped product with many small tenants. |
| **Weighted fair queuing (WFQ)** | Requires pre-configuring weights per tenant, which adds management overhead. Round-robin is simpler and works well when tenants have roughly equal priority. |
| **Priority queues with tenant quotas** | Risk of priority inversion and starvation if quotas are misconfigured. Hard quotas are brittle and often wrong under variable load. |
| **Separate worker pools per tenant** | Underutilizes expensive GPU resources during low-traffic periods for a tenant. |

---

## Consequences

### Positive

- **Strong fairness guarantees:** Every active tenant gets an equal share of worker time, and every user within a tenant gets an equal share of their tenant's turn. This prevents starvation entirely under normal load.
- **Bounded memory:** The internal queue and per-user deques have explicit limits, preventing memory exhaustion.
- **High utilization:** The next-fit worker assignment ensures tasks are dispatched as soon as a worker becomes available, keeping GPUs busy.
- **Graceful degradation under overload:** When buffers are full, tasks are nacked and redelivered later, preserving system stability rather than crashing.
- **Observability:** The dispatcher emits custom metrics (`ocr/dispatcher/tenant_fairness`, `ocr/dispatcher/user_fairness`) tracking the number of active tenants and users, which feed into dashboards.

### Negative

- **Increased complexity:** The dispatcher is a dedicated thread with its own synchronization primitives (`threading.Lock`, `threading.Condition`). Debugging fairness issues requires tracing task flow through multiple queues.
- **Extra latency for buffered tasks:** A task that arrives during a burst may wait in the tenant buffer while other tenants are served. However, this is the intended trade-off for fairness.
- **Round-robin granularity:** With many tenants (e.g., 50+), a tenant may wait through 49 other turns before getting a second task. For tenants with only occasional uploads, this is fine; for high-volume tenants, it could feel slow. Mitigated by the fact that real-world usage has a small number of active tenants at any given time.
- **Single dispatcher thread bottleneck:** The dispatcher is a single Python thread. Under extremely high throughput (hundreds of tasks per second), it could become CPU-bound. Current load is well within safe limits, but this would need refactoring if scale increases 10x.

---

## References

- Google Cloud Pub/Sub flow control: https://cloud.google.com/pubsub/docs/pull#flow_control
- Round-robin scheduling theory
- Multi-tenant isolation patterns in SaaS (Todd Hoff, "Designing Data-Intensive Applications" – tenant isolation patterns)