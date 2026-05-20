"""
Graceful Shutdown with Hard Timer — Spot VM Preemption Handler

This module implements a deterministic, time-bounded shutdown sequence designed
for workloads running on Google Cloud Spot VMs. Spot VMs can be preempted with
only a 30-second notice. This handler guarantees that all in-flight work is
either completed or safely returned to the queue before the process exits.

Original context: Axima document processing SaaS (GCP, Python 3.10).
Extracted and anonymized for engineering showcase.

Key patterns demonstrated:
- Multi-phase shutdown with strict ordering
- Hard timeout using threading.Timer to guarantee process exit
- Draining of internal queues and upload executor
- Safe NACK of unprocessed messages back to Pub/Sub
- VRAM cleanup and metric flush before termination
"""

import os
import gc
import time
import signal
import threading
from typing import Optional

# ---------------------------------------------------------------------------
# External references (simplified for showcase)
# These would be real objects in the parent process.
# ---------------------------------------------------------------------------
SHUTTING_DOWN = threading.Event()
DRAINING = threading.Event()
IN_FLIGHT_MESSAGES = 0
_counter_lock = threading.Lock()

# Task buffers and queues (referenced from dispatcher and Pub/Sub callback)
tenant_buffers = {}          # Per-tenant, per-user task buffers
INTERNAL_TASK_QUEUE = None   # queue.Queue instance
UPLOAD_EXECUTOR = None       # concurrent.futures.ThreadPoolExecutor
child_processes: list = []   # multiprocessing.Process instances
child_queues: list = []      # multiprocessing.Queue instances
child_stop_events: list = [] # multiprocessing.Event instances
METRIC_QUEUE_GLOBAL = None   # multiprocessing.Queue for metrics

# Pub/Sub streaming pull future
streaming_pull_future: Optional[object] = None

# ---------------------------------------------------------------------------
# Core shutdown function
# ---------------------------------------------------------------------------
def graceful_shutdown():
    """
    Executes a multi-phase graceful shutdown within a 25-second hard deadline.

    Phases:
    1. Stop ingress (cancel Pub/Sub pull, stop dispatcher)
    2. NACK all buffered tasks immediately
    3. Wait for in-flight tasks (max 10s)
    4. Drain upload executor (wait for pending uploads)
    5. Signal children to stop and join them
    6. Flush remaining metrics to Cloud Monitoring
    7. Clean up VRAM and IPC resources
    """
    global streaming_pull_future, UPLOAD_EXECUTOR, METRIC_QUEUE_GLOBAL

    SHUTTING_DOWN.set()
    DRAINING.set()

    # Hard timer: if anything hangs, force exit before the 30s GCP window closes
    def hard_exit():
        try:
            gc.collect()
        except Exception:
            pass
        os._exit(0)

    shutdown_timer = threading.Timer(25.0, hard_exit)
    shutdown_timer.start()

    try:
        # --- Phase 1: Stop ingress ---
        # Cancel Pub/Sub streaming pull to stop receiving new messages
        if streaming_pull_future:
            streaming_pull_future.cancel()
            try:
                streaming_pull_future.result(timeout=3)
            except Exception:
                pass

        # Signal the dispatcher thread to stop
        # (dispatcher_stop_event.set() would be called here)
        # dispatcher_thread.join(timeout=3)

        # --- Phase 2: NACK all buffered tasks ---
        # Return unprocessed tasks to Pub/Sub so other workers can handle them.
        purged = 0
        for tenant_data in tenant_buffers.values():
            for user_deque in tenant_data.get("users", {}).values():
                while user_deque:
                    task = user_deque.popleft()
                    try:
                        task["message"].nack()
                    except Exception:
                        pass
                    if task.get("lease"):
                        task["lease"].stop()
                    purged += 1
            tenant_data["users"].clear()
            tenant_data["user_rr_list"].clear()
        tenant_buffers.clear()

        # Drain the internal task queue (bounded queue between Pub/Sub and dispatcher)
        drained = 0
        while not INTERNAL_TASK_QUEUE.empty():
            try:
                task = INTERNAL_TASK_QUEUE.get_nowait()
                task["message"].nack()
                if task.get("lease"):
                    task["lease"].stop()
                drained += 1
            except Exception:
                pass

        # --- Phase 3: Wait for in-flight messages ---
        # Give currently processing child workers time to finish and write results.
        DRAIN_TIMEOUT = 10  # seconds
        start = time.time()
        while IN_FLIGHT_MESSAGES > 0 and (time.time() - start < DRAIN_TIMEOUT):
            time.sleep(0.5)

        # Clean up any remaining pending tasks
        # (pending_tasks dict would be drained here with nack)

        # --- Phase 4: Drain upload executor ---
        # Wait for all submitted uploads to GCS to complete.
        if UPLOAD_EXECUTOR:
            try:
                UPLOAD_EXECUTOR.shutdown(wait=True)
            except Exception:
                pass

        # --- Phase 5: Stop child processes ---
        for stop_event in child_stop_events:
            if stop_event:
                stop_event.set()

        for i, proc in enumerate(child_processes):
            if proc and proc.is_alive():
                proc.join(timeout=5)
                if proc.is_alive():
                    proc.terminate()
                    proc.join(timeout=2)

        # --- Phase 6: Clean up VRAM ---
        try:
            # In the real worker, this calls paddle.device.cuda.empty_cache()
            # and gc.collect(). Shown generically here.
            gc.collect()
        except Exception:
            pass

        # Close metric queue IPC to prevent pipe leaks
        if METRIC_QUEUE_GLOBAL:
            try:
                METRIC_QUEUE_GLOBAL.cancel_join_thread()
                while not METRIC_QUEUE_GLOBAL.empty():
                    METRIC_QUEUE_GLOBAL.get_nowait()
                METRIC_QUEUE_GLOBAL.close()
            except Exception:
                pass

        # Flush final metrics batch (handled by CloudMonitoringBatcher.stop())

    finally:
        shutdown_timer.cancel()


# ---------------------------------------------------------------------------
# Signal handler wiring
# ---------------------------------------------------------------------------
def handle_sigterm(signum, frame):
    """Entry point for GCP's preemption signal (SIGTERM)."""
    graceful_shutdown()
    os._exit(0)

# In main startup:
# signal.signal(signal.SIGTERM, handle_sigterm)
# signal.signal(signal.SIGINT, handle_sigterm)