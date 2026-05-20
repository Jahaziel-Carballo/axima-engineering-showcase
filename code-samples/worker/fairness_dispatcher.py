"""
Multi-tenant Fairness Dispatcher — Hierarchical Round-Robin

This module implements a two-level round-robin task dispatcher designed for
GPU worker pools serving multiple tenants and users. It prevents noisy-neighbor
starvation by ensuring every tenant gets an equal share of worker time, and
every user within a tenant gets fair access to that tenant's turn.

Original context: Axima document processing SaaS (GCP, Python 3.10).
Extracted and anonymized for engineering showcase.

Key patterns demonstrated:
- Hierarchical fairness (tenant → user round-robin)
- Bounded internal queues with backpressure
- Non-blocking, next-fit worker assignment
- Event-driven dispatching via threading.Condition
- Graceful degradation when buffers are full
"""

import queue
import threading
from collections import deque
from typing import Dict, Optional

# ---------------------------------------------------------------------------
# Configuration (normally injected via environment variables)
# ---------------------------------------------------------------------------
NUM_CHILDREN = 3           # Number of GPU worker processes
MAX_BUFFERED_TASKS = 100    # Global limit to prevent unbounded memory growth

# ---------------------------------------------------------------------------
# Shared state (lives in the parent process, shared with dispatcher thread)
# ---------------------------------------------------------------------------
tenant_buffers: Dict[str, Dict] = {}   # {tenant_id: {"users": {...}, "user_rr_list": deque}}
tenant_lock = threading.Lock()

INTERNAL_TASK_QUEUE = queue.Queue(maxsize=NUM_CHILDREN * 10)

dispatcher_stop_event = threading.Event()
dispatcher_condition = threading.Condition()

child_queues: list[queue.Queue] = []        # One bounded Queue per child process
child_processes: list[Optional[object]] = [] # Process handles (for liveness checks)
last_child_used = -1

# ---------------------------------------------------------------------------
# Helper: find an available child with next-fit strategy
# ---------------------------------------------------------------------------
def find_available_child(start_index: int = 0) -> Optional[int]:
    """
    Scans child workers circularly from start_index.
    Returns the index of the first alive child whose queue is not full.
    """
    for offset in range(NUM_CHILDREN):
        i = (start_index + offset) % NUM_CHILDREN
        p = child_processes[i]
        if p is None or not p.is_alive():
            continue
        if not child_queues[i].full():
            return i
    return None

# ---------------------------------------------------------------------------
# Main dispatcher loop (runs in a dedicated daemon thread)
# ---------------------------------------------------------------------------
def dispatcher_loop():
    """
    Consumes tasks from the internal queue, buffers them per-tenant/per-user,
    and assigns them to GPU workers using two-level round-robin.
    """
    global last_child_used
    local_tenant_idx = 0

    while not dispatcher_stop_event.is_set():

        # --- Phase A: Ingest new tasks from internal queue into buffers ---
        try:
            new_task = INTERNAL_TASK_QUEUE.get(timeout=0.1)

            # Global buffer cap — prevent unbounded growth
            with tenant_lock:
                total_buffered = sum(
                    len(user_deque)
                    for t_data in tenant_buffers.values()
                    for user_deque in t_data["users"].values()
                )
            if total_buffered >= MAX_BUFFERED_TASKS:
                new_task["message"].nack()
                if new_task.get("lease"):
                    new_task["lease"].stop()
                continue

            tenant_id = new_task["doc_ref_info"]["tenant_id"]
            user_id = new_task["doc_ref_info"]["user_id"]

            with tenant_lock:
                if tenant_id not in tenant_buffers:
                    tenant_buffers[tenant_id] = {
                        "users": {},
                        "user_rr_list": deque()
                    }
                t_data = tenant_buffers[tenant_id]
                if user_id not in t_data["users"]:
                    t_data["users"][user_id] = deque()
                    t_data["user_rr_list"].append(user_id)
                t_data["users"][user_id].append(new_task)

        except queue.Empty:
            pass  # No new tasks; proceed to dispatch phase

        # --- Phase B: Select next task with tenant→user fairness ---
        with tenant_lock:
            active_tenants = [
                tid for tid, data in tenant_buffers.items()
                if data["users"]
            ]

        if not active_tenants:
            with dispatcher_condition:
                dispatcher_condition.wait(timeout=0.5)
            continue

        # Round-robin across tenants
        target_tenant = active_tenants[local_tenant_idx % len(active_tenants)]
        local_tenant_idx += 1

        with tenant_lock:
            t_data = tenant_buffers.get(target_tenant)
            if not t_data or not t_data["user_rr_list"]:
                continue

            task = None
            uid = None
            try:
                uid = t_data["user_rr_list"].popleft()
                user_queue = t_data["users"].get(uid)
                if user_queue and user_queue:
                    task = user_queue.popleft()
                    # Re-queue user if they still have pending tasks
                    if user_queue:
                        t_data["user_rr_list"].append(uid)
                    else:
                        del t_data["users"][uid]
                else:
                    if uid in t_data["users"]:
                        del t_data["users"][uid]
            except IndexError:
                continue

        if not task:
            continue

        # --- Phase C: Assign task to an available child worker ---
        start_idx = (last_child_used + 1) % NUM_CHILDREN if last_child_used != -1 else 0
        child_id = find_available_child(start_idx)

        if child_id is not None:
            try:
                # Start the lease maintainer just before dispatching
                lease = task.get("lease")
                if lease:
                    lease.start()

                child_queues[child_id].put_nowait(task)
                last_child_used = child_id

                # Track in-flight tasks for shutdown coordination
                with threading.Lock():  # pending_lock (simplified)
                    pass  # pending_tasks[task["trace_id"]] = (message, lease, event, child_id)

            except queue.Full:
                # Worker queue full — return task to its user queue for retry
                with tenant_lock:
                    if target_tenant in tenant_buffers and uid in tenant_buffers[target_tenant]["users"]:
                        tenant_buffers[target_tenant]["users"][uid].appendleft(task)
                with dispatcher_condition:
                    dispatcher_condition.wait(timeout=0.1)
        else:
            # No worker available — return task and wait
            with tenant_lock:
                if target_tenant in tenant_buffers and uid in tenant_buffers[target_tenant]["users"]:
                    tenant_buffers[target_tenant]["users"][uid].appendleft(task)
            with dispatcher_condition:
                dispatcher_condition.wait(timeout=0.1)