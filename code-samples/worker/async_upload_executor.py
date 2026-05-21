"""
Async Upload Executor — Bounded Concurrency for Cloud Storage & Firestore

This module implements an asynchronous result persistence layer that decouples
the Pub/Sub callback from slow I/O operations (GCS uploads, Firestore writes).
It uses a dedicated ThreadPoolExecutor with a BoundedSemaphore to limit the
number of concurrent uploads, preventing thundering‑herd pressure on cloud APIs.

Original context: Axima document processing SaaS (GCP, Python 3.10).
Extracted and anonymized for engineering showcase.

Key patterns demonstrated:
- Decoupling callback from I/O via thread pool
- Bounded concurrency with BoundedSemaphore
- Graceful degradation under saturation (nack, retry later)
- Clean shutdown: drain executor, flush remaining uploads
- Separation of concerns: callback handles orchestration, executor handles persistence
"""

import threading
import time
import json
from concurrent.futures import ThreadPoolExecutor

# ---------------------------------------------------------------------------
# Globals (normally initialized once at worker startup)
# ---------------------------------------------------------------------------
# GCP clients
storage_client = None          # google.cloud.storage.Client
firestore_client = None        # google.cloud.firestore.Client

# Executor and semaphore
UPLOAD_EXECUTOR = ThreadPoolExecutor(max_workers=20, thread_name_prefix="upload")
UPLOAD_SEMAPHORE = threading.BoundedSemaphore(value=50)

# Shutdown flag (set when SIGTERM is received)
SHUTTING_DOWN = threading.Event()


# ---------------------------------------------------------------------------
# Core upload function (runs inside a worker thread)
# ---------------------------------------------------------------------------
def upload_result_and_finalize(
    tenant_id: str,
    user_id: str,
    doc_id: str,
    result_payload: dict,
    doc_ref,          # Firestore document reference
    message,           # Pub/Sub message object
    lease              # LeaseMaintainer instance
):
    """
    Uploads the processing result to Cloud Storage, updates Firestore,
    and acknowledges the Pub/Sub message. On failure, nacks the message
    and stops the lease, ensuring the document will be retried.

    This function is submitted to UPLOAD_EXECUTOR and runs asynchronously.
    """
    try:
        # 1. Upload result JSON to Cloud Storage
        result_path = f"tenants/{tenant_id}/{user_id}/{doc_id}/analysis.json"
        bucket = storage_client.bucket(RESULTS_BUCKET)  # injected elsewhere
        blob = bucket.blob(result_path)
        blob.upload_from_string(
            json.dumps(result_payload, ensure_ascii=False),
            content_type='application/json'
        )

        # 2. Update Firestore with final status
        doc_ref.update({
            "status": "PROCESSED",
            "gcs_result_uri": f"gs://{RESULTS_BUCKET}/{result_path}",
            "page_count": result_payload.get("page_count", 0),
            "processing_time_sec": result_payload.get("processing_time", 0),
            "processed_at": firestore_client.SERVER_TIMESTAMP
        })

        # 3. Acknowledge the Pub/Sub message
        message.ack()
        if lease:
            lease.stop()

    except Exception:
        # On any failure, nack the message and stop the lease
        try:
            message.nack()
        except Exception:
            pass
        if lease:
            lease.stop()
    finally:
        # Release the semaphore so another upload can be queued
        UPLOAD_SEMAPHORE.release()


# ---------------------------------------------------------------------------
# Integration point: called from the Pub/Sub callback
# ---------------------------------------------------------------------------
def on_document_processed(result_data: dict, message, doc_ref, lease):
    """
    Called by the Pub/Sub callback once a child process has written
    its result to /dev/shm. Attempts to schedule the upload.
    If the upload pipeline is saturated, nacks the message immediately
    to return it to Pub/Sub for later redelivery.
    """
    # Non‑blocking acquire: if no permits available, reject gracefully
    if not UPLOAD_SEMAPHORE.acquire(blocking=False):
        # Upload pipeline full → nack and stop lease
        message.nack()
        if lease:
            lease.stop()
        return

    # Submit the upload work to the thread pool
    UPLOAD_EXECUTOR.submit(
        upload_result_and_finalize,
        result_data["tenant_id"],
        result_data["user_id"],
        result_data["doc_id"],
        result_data["payload"],
        doc_ref,
        message,
        lease
    )
    # The callback returns immediately without waiting for the upload.


# ---------------------------------------------------------------------------
# Graceful shutdown integration
# ---------------------------------------------------------------------------
def drain_upload_executor():
    """
    Called during graceful shutdown (SIGTERM handler).
    Waits for all running upload tasks to complete, then shuts down the pool.
    """
    # Signal that no new uploads will be accepted
    # (in practice, the Pub/Sub subscription is already cancelled by this point)

    # Wait for all submitted uploads to finish
    UPLOAD_EXECUTOR.shutdown(wait=True)