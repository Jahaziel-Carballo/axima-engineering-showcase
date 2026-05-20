# ADR-005: Guaranteed ordering and exactly-once processing with Pub/Sub ordered delivery + Firestore optimistic locking

- **Status:** Accepted
- **Date:** 2025-04-05
- **Deciders:** Jahaziel Carballo (Solo Developer)

---

## Context

Axima processes batches of documents uploaded by accounting firms. Within a batch, documents often have dependencies (e.g., a cover letter followed by detailed reports). While each document is processed independently, **maintaining submission order for metadata and audit trails** is critical for user trust.

Additionally, Pub/Sub guarantees at-least-once delivery, which means:
- A message can be redelivered if the ack deadline expires.
- Network glitches or worker restarts can cause duplicate deliveries.

Without proper handling, duplicate messages could result in:
- Double processing of the same document (wasting GPU cycles).
- Firestore writes overwriting a previously successful result with a duplicate.
- Confusing status transitions (e.g., a document marked `PROCESSED` being overwritten by a later `PROCESSING` update).

The challenge was to achieve **exactly-once processing semantics** and **ordered delivery within a tenant** while keeping the system reactive and high-throughput.

---

## Decision

**Use Pub/Sub's ordered delivery feature with ordering keys, combined with Firestore optimistic locking via transactions and heartbeat timestamps.**

### Implementation details

1. **Pub/Sub ordering key:** When the API orchestrator publishes a document message, it sets the `ordering_key` to `{tenant_id}/{batch_id}`. This ensures that all documents within the same tenant batch are delivered to the worker in submission order, and that they are processed sequentially (or at least their metadata updates are ordered).

2. **Firestore lock document (`get_and_lock_document` transaction):**
   - A transactional read-modify-write operation on the Firestore document.
   - If the document status is already `PROCESSED` or `FAILED`, the transaction returns `ALREADY_DONE`, and the message is acked immediately without reprocessing.
   - If the status is `PROCESSING` and the `last_heartbeat` timestamp is recent (< 5 minutes), the document is considered locked by another worker, and the message is nacked for redelivery.
   - If the status is `PROCESSING` but the heartbeat is stale (> 5 minutes), the lock is stolen, and the worker proceeds (handles zombie worker scenarios).
   - If the status is `PENDING`, the worker atomically updates it to `PROCESSING` with a fresh heartbeat and proceeds.

3. **Lease maintainer thread:** While processing, a background thread extends the Pub/Sub ack deadline every 45 seconds and updates the Firestore heartbeat, preventing other workers from stealing the lock prematurely.

4. **Ordered callback execution:** Pub/Sub ordered delivery ensures that for a given ordering key, the next message is not delivered until the previous one is acked or its ack deadline expires. This naturally throttles processing to one document at a time per ordering key, preventing race conditions on Firestore updates.

5. **Idempotency in uploads:** Even if a result is uploaded to GCS multiple times (edge case), the final Firestore transaction checks the status again before writing, acting as a final guard against double processing.

---

## Alternatives considered

| Alternative | Rejected because |
|-------------|------------------|
| **No ordering, no locking** | Chaos: documents processed out of order, duplicates overwriting each other, impossible to audit. |
| **Dedicated per-tenant worker queues** | Explodes cost and complexity. With 50+ tenants, you'd need 50+ subscriptions and worker pools. |
| **Firestore distributed locks with `google-cloud-firestore` native transactions** | Already using transactions, but Pub/Sub ordered delivery adds an extra layer of protection at the transport layer. |
| **Redis locks (external)** | Adds infrastructure dependency. Firestore transactions are sufficient for this workload and are already in the stack. |
| **Idempotent processing via content hashing** | Could work, but requires reading the document first to hash it, which adds latency. Locking by document ID is simpler and faster. |

---

## Consequences

### Positive

- **Exactly-once processing:** No document is processed twice, even under redelivery or worker crashes.
- **Ordered metadata:** Audit trails show documents processed in the order they were submitted, matching user expectations.
- **Resilience to worker failures:** Stale locks are automatically stolen after 5 minutes, so a zombie worker cannot block a document indefinitely.
- **GCP-native:** Uses Pub/Sub and Firestore features as designed, avoiding external dependencies.
- **Cost-efficient:** Ordering keys add no extra cost; they are a free feature of Pub/Sub.

### Negative

- **Ordering key throughput limit:** Pub/Sub limits ordered delivery to 1 MBps per ordering key. For very large batches with huge documents, this could become a bottleneck. Mitigated by the fact that Axima processes documents individually (max 15 MB each), and the pipeline is asynchronous, so bursts are spread over time.
- **Head-of-line blocking:** If a document fails to process and is nacked repeatedly, the entire ordering key is blocked until that document succeeds or reaches max retries and goes to the DLQ. Mitigated by the DLQ and the 5-retry limit.
- **Firestore transaction contention:** Multiple workers attempting to claim the same document simultaneously will cause transaction aborts. The retry wrapper handles this gracefully, but under extreme contention (many workers, few documents), it could add latency.
- **Ordering key cardinality:** If a tenant uploads many small batches with different batch IDs, the ordering key cardinality grows, reducing the blocking risk but also making order guarantees looser. This is acceptable because users expect ordering within a batch, not across all time.

---

## References

- Google Cloud Pub/Sub ordering: https://cloud.google.com/pubsub/docs/ordering
- Firestore transactions: https://cloud.google.com/firestore/docs/manage-data/transactions
- Exactly-once processing patterns in GCP