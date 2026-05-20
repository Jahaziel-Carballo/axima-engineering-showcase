# ADR-004: Asynchronous upload pipeline with bounded concurrency

- **Status:** Accepted
- **Date:** 2025-03-25
- **Deciders:** Jahaziel Carballo (Solo Developer)

---

## Context

Once a GPU worker finishes processing a document, the result (a JSON payload typically 2–15 MB) must be:

1. Uploaded to Cloud Storage under `gs://{RESULTS_BUCKET}/tenants/{tenant}/{user}/{doc_id}/analysis.json`.
2. Recorded in Firestore with status `PROCESSED`, a timestamp, the GCS URI, and processing metadata.
3. Acknowledged (acked) on Pub/Sub so the message is not redelivered.

The Pub/Sub callback that orchestrates this flow has a 600-second ack deadline, but waiting for the upload and Firestore update synchronously inside that callback would waste a thread that could be pulling new messages. Under high load, this would slow down ingestion and reduce overall throughput.

Additionally, both GCS and Firestore have client-side upload limits and latency variability. A burst of large documents could trigger a "thundering herd" of parallel uploads, potentially hitting quota limits or overwhelming the Firestore write rate.

The challenge was to design a result-persistence mechanism that:

- Releases the Pub/Sub callback thread as soon as the result is ready, without waiting for upload completion.
- Limits the number of concurrent upload operations to protect GCS and Firestore.
- Guarantees that no successfully processed result is lost, even during a Spot VM preemption.
- Handles upload failures gracefully, nacking the message so it can be retried.

---

## Decision

**Implement an asynchronous upload pipeline using a `ThreadPoolExecutor` with a bounded semaphore, a separate upload function that handles ACK/NACK, and a drain procedure during graceful shutdown.**

### Architecture

1. **ThreadPoolExecutor (`UPLOAD_EXECUTOR`):** A global executor with 20 workers (`max_workers=20`) dedicated to result uploads. It is created once at worker startup and survives for the lifetime of the worker process.

2. **Bounded semaphore (`UPLOAD_SEMAPHORE`):** A `threading.BoundedSemaphore` with value 50, which limits how many upload tasks can be queued at once. Before submitting an upload task, the callback acquires the semaphore (non-blocking). If the semaphore is exhausted, the message is nacked, and the lease is stopped, returning the document to Pub/Sub for later redelivery.

3. **Upload function (`_upload_result_to_gcs_and_firestore`):** This function is executed by the executor threads. It:
   - Uploads the JSON to GCS.
   - Updates Firestore with the result metadata.
   - Acks the Pub/Sub message on success.
   - Stops the LeaseMaintainer (which otherwise extends the ack deadline).
   - On failure, nacks the message and stops the lease, ensuring the document will be retried.
   - Releases the semaphore in a `finally` block.

4. **Callback decoupling:** The Pub/Sub callback:
   - Reads the result from `/dev/shm` (see ADR-001).
   - Acquires the upload semaphore.
   - Submits the upload function to the executor.
   - Returns immediately, without waiting for the upload to finish. This keeps the callback responsive and allows new messages to be pulled.

5. **Graceful shutdown integration:** During the worker's `graceful_shutdown()` sequence (see ADR-002), after stopping the Pub/Sub subscription and draining task buffers, the procedure:
   - Calls `UPLOAD_EXECUTOR.shutdown(wait=True)` to wait for all running upload tasks to complete.
   - Any tasks still queued but not yet running are cancelled; their messages remain nacked (they were nacked when they were removed from the internal queue during shutdown), so Pub/Sub will redeliver them.

### Why bounded concurrency matters

Without a semaphore, an influx of completed documents could submit hundreds of upload tasks to the executor's internal queue, consuming memory and risking GCS quota exhaustion. The semaphore cap of 50 acts as a pressure valve: when the upload pipeline is saturated, new results are gently rejected (nacked) and will be retried after the backlog clears.

---

## Alternatives considered

| Alternative | Rejected because |
|-------------|------------------|
| **Synchronous upload in the callback** | Blocks the callback, reducing throughput and risking ack deadline misses for long uploads. |
| **Separate Pub/Sub topic for results** | Adds another hop, more complexity, and higher latency. The result is already in memory; directly uploading is simpler. |
| **Cloud Run Jobs for uploads** | Overkill for a simple GCS write. Adds cost and cold-start latency. |
| **Upload with unlimited concurrency** | Risk of hitting GCS quota limits (e.g., 100 write requests per second per bucket) and Firestore write limits (1 write per second per document). Bounded concurrency prevents this. |
| **Using `multiprocessing` instead of threads for uploads** | Uploads are I/O-bound, not CPU-bound. Threads are lighter and perfectly suited for this task. |

---

## Consequences

### Positive

- **High callback throughput:** The Pub/Sub callback spends minimal time per message, enabling the worker to pull and dispatch new documents continuously.
- **Controlled resource usage:** The bounded semaphore caps both memory consumption (pending uploads) and API pressure on GCS/Firestore.
- **Graceful degradation under saturation:** When the upload pipeline is full, messages are nacked instead of crashing the worker. Pub/Sub redelivery naturally smooths out bursts.
- **Reliable shutdown:** The executor drain guarantees that all in-flight uploads complete before the process exits, preventing data loss during Spot VM preemptions.
- **Separation of concerns:** The callback handles orchestration; the upload function handles persistence. This makes both easier to test and debug.

### Negative

- **Potential duplicate processing:** If a message is nacked due to upload saturation, it will be redelivered and reprocessed, consuming GPU cycles twice. This is rare and acceptable given the cost savings of the overall architecture.
- **Upload executor queue monitoring:** The health endpoint reports `upload_executor_pending` to track saturation, but if monitoring is broken, the operator could miss a steadily growing backlog.
- **Semaphore fairness:** The semaphore is first-come-first-served; it does not enforce tenant fairness at the upload level. However, fairness in processing (ADR-003) ensures that documents from different tenants are interleaved before they reach the upload pipeline, so no single tenant can monopolize upload slots.
- **Potential Firestore contention:** Although the semaphore limits total uploads, many updates targeting the same Firestore document could still cause contention. In practice, each upload writes to a unique document (`doc_id`), so this is not an issue.

---

## References

- Python `concurrent.futures.ThreadPoolExecutor`: https://docs.python.org/3/library/concurrent.futures.html
- Google Cloud Storage quotas and limits: https://cloud.google.com/storage/quotas
- Firestore write limits and contention: https://cloud.google.com/firestore/docs/best-practices
- GCP Spot VM preemption window and best practices